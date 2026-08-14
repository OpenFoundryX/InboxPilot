# AWS operations

Day-to-day access to the running stack: logs, shells, addresses, and the
database. For first deploy and cutover see `aws-deploy.md`.

Everything below assumes `--region ap-south-1` and cluster `inboxpilot`. Set
these once per shell to shorten every command:

```bash
export AWS_REGION=ap-south-1
export CLUSTER=inboxpilot
```

## Logs

Three log groups, 14-day retention:

| Group | Contents |
|---|---|
| `/ecs/inboxpilot/api` | uvicorn, request handling, startup warnings |
| `/ecs/inboxpilot/worker` | Celery worker and the embedded beat scheduler |
| `/ecs/inboxpilot/migrate` | one-shot Alembic runs from the deploy pipeline |

```bash
# Follow live
aws logs tail /ecs/inboxpilot/api --follow

# Recent history
aws logs tail /ecs/inboxpilot/worker --since 1h

# Filter — the log lines are structured, so grep on the event name
aws logs tail /ecs/inboxpilot/api --since 30m --filter-pattern "ERROR"
aws logs tail /ecs/inboxpilot/worker --since 1h | grep "Sending due task"
```

The migrate group only has entries when a deploy ran migrations. An empty group
after a failed deploy means it failed *before* the task started — check the
GitHub Actions log instead.

## Task addresses

Fargate tasks get a new IP on every deploy, so never hardcode one. Look it up:

```bash
# Task ARN for a service
aws ecs list-tasks --cluster $CLUSTER --service-name inboxpilot-api \
  --query 'taskArns[0]' --output text

# Private IP (inside the VPC)
TASK=$(aws ecs list-tasks --cluster $CLUSTER --service-name inboxpilot-api \
  --query 'taskArns[0]' --output text)
aws ecs describe-tasks --cluster $CLUSTER --tasks "$TASK" \
  --query 'tasks[0].attachments[0].details[?name==`privateIPv4Address`].value' \
  --output text

# Public IP (tasks run in public subnets — this is how they reach ECR and
# every third-party API without a NAT gateway)
ENI=$(aws ecs describe-tasks --cluster $CLUSTER --tasks "$TASK" \
  --query 'tasks[0].attachments[0].details[?name==`networkInterfaceId`].value' \
  --output text)
aws ec2 describe-network-interfaces --network-interface-ids "$ENI" \
  --query 'NetworkInterfaces[0].Association.PublicIp' --output text
```

The public IP is **not** an entry point — the task security group only accepts
port 8000 from the ALB. It is outbound identity, which is what matters if a
third party ever asks you to allowlist an address. It changes on every deploy,
so an allowlist based on it will break; ask for a domain-based rule instead, or
we add a NAT gateway with an Elastic IP (~$32/mo).

Public traffic always goes through `https://api.inboxoshq.com`.

## Shell into a container

ECS Exec is enabled on both services. Requires the Session Manager plugin
locally (`brew install --cask session-manager-plugin`).

```bash
TASK=$(aws ecs list-tasks --cluster $CLUSTER --service-name inboxpilot-api \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster $CLUSTER --task "$TASK" \
  --container api --interactive --command "/bin/bash"
```

Use `--container worker` and `--service-name inboxpilot-worker` for the worker.

Inside, the app's environment is fully populated, so anything the app can do you
can do:

```bash
alembic current                 # schema revision
python -c "from core.config import settings; print(settings.DATABASE_URL)"
```

`Cannot perform start session: EOF` at the end of a non-interactive
`--command` is normal — the command ran, the session just closed.

## Database

**The database is not reachable from your laptop, by design.** It sits in a
private subnet with `publicly_accessible = false`, and its security group
accepts 5432 only from the task security group. `psql` from outside times out
against a `10.20.x.x` address because that address does not route off the VPC.

### Option 1 — psql inside a task (no new infrastructure)

The image has no Postgres client, but the task has egress, so install one for
the life of that container:

```bash
TASK=$(aws ecs list-tasks --cluster $CLUSTER --service-name inboxpilot-api \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster $CLUSTER --task "$TASK" \
  --container api --interactive --command "/bin/bash"

# then, inside:
apt-get update && apt-get install -y postgresql-client
psql "$(python -c "from core.config import settings; \
  print(settings.DATABASE_URL.replace('+asyncpg',''))")"
```

Fine for a quick query. The install disappears on the next deploy, and it costs
nothing.

### Option 2 — SSM port-forward through a bastion (proper psql from your laptop)

Needs a `t4g.nano` EC2 instance in a public subnet with the SSM agent and a
security group allowed into the RDS group — roughly **$3–4/month**. No SSH key
and no open inbound ports; Session Manager tunnels it:

```bash
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["inboxpilot.c1e6wecyse69.ap-south-1.rds.amazonaws.com"],
                 "portNumber":["5432"],"localPortNumber":["5432"]}'

# then, in another shell, exactly the command that timed out before:
psql "host=localhost port=5432 dbname=inboxos user=inboxos_user"
```

This is not built yet. Ask and it is ~40 lines of Terraform.

### Option 3 — direct access (currently ENABLED)

`db_publicly_accessible = true` in `terraform.tfvars`, with the security group
scoped to the addresses in `db_allowed_cidrs`. Connect straight from a laptop:

```bash
psql "host=$(cd infra && terraform output -raw db_address) port=5432 \
      dbname=inboxos user=inboxos_user sslmode=require"
```

The password is in SSM (see below).

Two things this involves that are easy to miss:

- **The DB subnet group moves to the public subnets.** RDS only assigns a
  reachable public address in a subnet with a route to an internet gateway; left
  in the private subnets, `publicly_accessible = true` yields an endpoint that
  resolves and then times out, which looks exactly like a firewall problem.
- **The security group is the only protection.** Behind it is one password
  guarding users' Google OAuth tokens, mail content and billing records. The
  `db_allowed_cidrs` validation rejects `0.0.0.0/0`, but a stale home IP in that
  list is someone else's address now.

**Turn it off when the work is done:**

```hcl
db_publicly_accessible = false
db_allowed_cidrs       = []
```

then `terraform apply`. For ongoing access prefer Option 2 — the bastion gives
the same psql session for ~$3/month with nothing exposed. It is already written
in `infra/bastion.tf`; flip `bastion_enabled = true`.

### The password

Generated by Terraform and stored in two places: the `DATABASE_URL` SSM
parameter, and Terraform state.

```bash
aws ssm get-parameter --name /inboxpilot/prod/DATABASE_URL \
  --with-decryption --query 'Parameter.Value' --output text
```

## RabbitMQ

The Celery broker. A single Fargate task, reached at
`rabbitmq.inboxpilot.local:5672` through Cloud Map, with its mnesia directory on
EFS so queued messages survive restarts and deploys.

Redis is no longer the broker. It keeps the cache, locks and rate limits on
db 0, and the Celery *result* backend on db 2.

```bash
# Is it up?
aws ecs describe-services --cluster $CLUSTER --services inboxpilot-rabbitmq \
  --query 'services[0].{running:runningCount,desired:desiredCount}' --output table

aws logs tail /ecs/inboxpilot/rabbitmq --since 30m
```

### Inspecting queues

The management UI on 15672 is deliberately not published. Use the CLI inside
the container:

```bash
TASK=$(aws ecs list-tasks --cluster $CLUSTER --service-name inboxpilot-rabbitmq \
  --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster $CLUSTER --task "$TASK" \
  --container rabbitmq --interactive --command "/bin/bash"

# inside:
rabbitmqctl list_queues name messages consumers
rabbitmqctl status
rabbitmq-diagnostics ping
```

`list_queues` showing a growing `messages` count with `consumers` at 0 means the
worker is not connected — check `/ecs/inboxpilot/worker` for connection errors
rather than restarting the broker.

### Things that will bite

🛑 **Never run two RabbitMQ tasks.** Both would mount the same EFS directory,
and the second either fails on a locked mnesia or corrupts it. The service is
pinned to `desired_count = 1` with `0/100` deployment percentages so the old
task always stops before the new one starts. That means **a deploy of the broker
is a brief broker outage** — Celery reconnects on its own
(`broker_connection_retry_on_startup`), and durable queues on EFS keep the
messages.

⚠️ **`RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` only apply on first
boot**, when the mnesia directory is empty. Once EFS holds a database, rotating
the SSM password does **not** change the broker's credentials — it just breaks
the clients. To actually rotate: change it inside the broker with
`rabbitmqctl change_password`, then update SSM to match.

⚠️ **`RABBITMQ_NODENAME` is pinned to `rabbit@inboxpilot`.** Left unset,
RabbitMQ derives the node name from the container hostname, which changes on
every task replacement — it would come up as a brand new empty node beside the
old data, and every queue would appear to have vanished.

### If the queue data is ever lost

Not fatal. Every scheduled task re-fires on its own interval — the worst case is
one missed sweep cycle. What does not survive is anything queued but not yet
processed, such as a booking confirmation email accepted a moment earlier.

## Redis

Same story — private subnet, task security group only. Reach it from inside a
task:

```bash
python -c "
import redis
from core.config import settings
r = redis.from_url(settings.REDIS_URL)
print(r.ping())
print('queued:', r.llen('celery'))
"
```

Cache is on db 0, the Celery broker on db 1, results on db 2.

## Restart and scale

```bash
# Restart a service without changing anything (pulls the same task definition)
aws ecs update-service --cluster $CLUSTER --service inboxpilot-api \
  --force-new-deployment

# Watch a rollout
aws ecs wait services-stable --cluster $CLUSTER --services inboxpilot-api

# Current state
aws ecs describe-services --cluster $CLUSTER \
  --services inboxpilot-api inboxpilot-worker \
  --query 'services[].{svc:serviceName,running:runningCount,desired:desiredCount}' \
  --output table
```

🛑 **Never raise `inboxpilot-worker` above 1.** Celery beat is embedded in it
(`-B`), and a second worker means a second scheduler firing every sweep twice.
Split beat into its own service at `desired_count = 1` first — see the comment
on `aws_ecs_service.worker` in `infra/ecs.tf`.

The API can be scaled freely:

```bash
aws ecs update-service --cluster $CLUSTER --service inboxpilot-api --desired-count 2
```

Terraform ignores `desired_count`, so a manual scale survives the next apply.

## The beat scheduler

Beat runs *inside* the worker container (`celery worker -B`), not as its own
service. One process, one scheduler. The schedule lives in
`src/beat_schedule.py`; a task listed there but missing from `TASK_MODULES` in
`src/worker.py` is dispatched and silently rejected as unknown, which has
happened before — the comments in `worker.py` name the two that were lost that
way.

### Is it alive?

```bash
aws logs tail /ecs/inboxpilot/worker --since 5m | grep "Sending due task"
```

Five tasks fire every minute (`mailman.tick`, `gmail.poll_all`,
`routines.sweep`, `reminders.sweep`, `meetings.sweep`), so silence for more than
a minute means beat is not running even if the container is up.

```bash
# Did the worker register its tasks at boot?
aws logs tail /ecs/inboxpilot/worker --since 30m | grep -A25 "\[tasks\]"
```

### The single-worker rule

🛑 **`inboxpilot-worker` must stay at `desired_count = 1`.** Two workers means
two embedded schedulers, and every sweep fires twice — duplicate briefings,
duplicate reminders, duplicate bots booked for the same meeting.

ECS makes this sharper than a normal host would. A default rolling deploy
(`100/200`) briefly runs the old and new task together, which is two schedulers
for the length of the changeover. That is why the worker service sets:

```hcl
deployment_minimum_healthy_percent = 0
deployment_maximum_percent         = 100
```

The old task stops before the new one starts. It costs about a minute with no
worker on each deploy; queued tasks wait in Redis and are consumed when the new
task comes up.

### Verifying no double-fire across a deploy

The failure only appears during a changeover, so check it there:

```bash
# Two distinct task IDs = a rollover happened in this window
aws logs tail /ecs/inboxpilot/worker --since 3h | awk '{print $2}' | sort -u

# No minute may contain two workers
aws logs tail /ecs/inboxpilot/worker --since 3h \
  | awk '{split($1,t,":"); print t[1]":"t[2], $2}' | sort -u \
  | awk '{print $1}' | uniq -c | awk '$1>1 {print "OVERLAP:", $2}'

# No task may fire twice in the same minute
aws logs tail /ecs/inboxpilot/worker --since 3h | grep "Sending due task" \
  | sed -E 's/.*\[([0-9-]+ [0-9]{2}:[0-9]{2}):.*Sending due task ([a-z-]+).*/\1 \2/' \
  | sort | uniq -c | awk '$1>1 {print "DUPLICATE:", $0}'
```

All three printing nothing is the pass condition. Verified clean on
2026-08-14 across a real deploy transition.

### Scaling the worker later

When one worker is no longer enough, beat must come out first:

1. Add an `aws_ecs_service.beat` + task definition running
   `celery -A worker.celery_app beat` at `desired_count = 1`, with the same
   `0/100` deployment percentages.
2. Change the worker's command from `worker -B` to plain `worker`.
3. Only then raise the worker count, and it can go back to `100/200`.

Roughly +$10/month for the extra task. Doing it in the other order double-fires
every sweep for as long as both workers run.

### Note on Gmail push

`GMAIL_PUSH_ENABLED` is `true` but `GOOGLE_PUBSUB_SA_EMAIL` is unset, so the
push endpoint accepts unauthenticated requests, and `main.py:51` logs
`gmail.push_unverified` on every boot. Per the comment in `beat_schedule.py`, an
org policy currently blocks Gmail's service account from publishing to the
topic, so push does not deliver anyway — `gmail-poll` at 60s is what actually
moves mail. If the boot warnings are noise, set `GMAIL_PUSH_ENABLED=false` until
push genuinely works; mail latency does not change.

## Which image is running

```bash
TD=$(aws ecs describe-services --cluster $CLUSTER --services inboxpilot-api \
  --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$TD" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text
```

The tag is the git SHA it was built from.

## Checking configuration

Non-secret config is plain environment on the task definition:

```bash
aws ecs describe-task-definition --task-definition inboxpilot-api \
  --query 'taskDefinition.containerDefinitions[0].environment' --output table
```

Secrets show only their SSM ARNs there, never values:

```bash
aws ssm get-parameters-by-path --path /inboxpilot/prod \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sort
```

⚠️ **Changing config takes two steps.** Terraform owns the task definition; CI
owns the image. `terraform apply` registers a new revision carrying the
placeholder tag `:bootstrap`, which does not exist in ECR — the service keeps
running the old revision until a deploy patches a real image onto the new
revision:

```bash
cd infra && terraform apply      # 1. new revision with the new config
cd .. && gh workflow run deploy.yml   # 2. real image onto it, roll the services
```

Skipping step 2 is why a config change can appear to have done nothing.
