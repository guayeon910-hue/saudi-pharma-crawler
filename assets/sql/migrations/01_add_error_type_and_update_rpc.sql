-- 01_add_error_type_and_update_rpc.sql
-- Idempotent migration: add error_type tracking + update RPC signature

-- 1) Add nullable column to avoid conflicts with existing rows.
alter table if exists public.saudi_failed_urls
  add column if not exists last_error_type text;

-- 2) Prevent RPC overloading by dropping exact signatures.
drop function if exists public.saudi_failed_urls_bump(text, text, text, text, integer);
drop function if exists public.saudi_failed_urls_bump(text, text, text, integer);

-- 3) Recreate RPC with error_type parameter.
create function public.saudi_failed_urls_bump(
    p_url text,
    p_source_name text,
    p_error text,
    p_error_type text,
    p_poison_threshold integer
) returns void
language plpgsql
as $$
begin
    insert into public.saudi_failed_urls (
        url,
        source_name,
        fail_count,
        last_error,
        last_error_type,
        first_failed_at,
        last_failed_at,
        dead
    ) values (
        p_url,
        p_source_name,
        1,
        p_error,
        p_error_type,
        now(),
        now(),
        false
    )
    on conflict (url) do update set
        fail_count       = public.saudi_failed_urls.fail_count + 1,
        last_error       = excluded.last_error,
        last_error_type  = excluded.last_error_type,
        last_failed_at   = now(),
        dead             = (public.saudi_failed_urls.fail_count + 1) >= p_poison_threshold;
end;
$$;

