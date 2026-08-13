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

### What NOT to do

Do not set `publicly_accessible = true` and open the security group to your IP.
It works, and it puts your production database on the public internet behind one
password, permanently, because nobody ever reverts it.

### The password

Generated by Terraform and stored in two places: the `DATABASE_URL` SSM
parameter, and Terraform state.

```bash
aws ssm get-parameter --name /inboxpilot/prod/DATABASE_URL \
  --with-decryption --query 'Parameter.Value' --output text
```

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
