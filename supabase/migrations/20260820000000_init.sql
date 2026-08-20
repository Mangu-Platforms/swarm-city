-- swarm-city initial schema
-- Stores task submissions, agent run results, and audit logs.

create table if not exists tasks (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  status      text not null default 'pending'
                check (status in ('pending','running','completed','failed')),
  prompt      text not null,
  result      text,
  score       numeric(4,2),
  metadata    jsonb not null default '{}'
);

create table if not exists agent_runs (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references tasks(id) on delete cascade,
  created_at  timestamptz not null default now(),
  agent_role  text not null,
  model       text,
  output      text,
  tokens_used integer,
  duration_ms integer
);

create table if not exists audit_log (
  id          bigserial primary key,
  created_at  timestamptz not null default now(),
  event       text not null,
  payload     jsonb not null default '{}'
);

-- updated_at trigger for tasks
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger tasks_updated_at
  before update on tasks
  for each row execute function set_updated_at();

-- Indexes
create index on tasks(status, created_at desc);
create index on agent_runs(task_id);
create index on audit_log(created_at desc);
