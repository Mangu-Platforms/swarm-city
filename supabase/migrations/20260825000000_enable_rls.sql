-- Enable row level security on all public tables.
-- No policies are defined, so anon/authenticated roles get no access;
-- the service role bypasses RLS and is the only intended writer.

alter table tasks enable row level security;
alter table agent_runs enable row level security;
alter table audit_log enable row level security;

-- Pin the trigger function's search_path (security linter 0011).
alter function public.set_updated_at() set search_path = '';
