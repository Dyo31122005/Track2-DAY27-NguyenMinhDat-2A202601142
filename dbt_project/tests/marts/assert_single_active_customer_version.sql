-- Singular business test: a customer can only have ONE currently-active
-- profile version at a time.
--
-- This is a business rule, not a column constraint: stg_customers is an
-- SCD-2 style dimension where old rows get closed out (is_active = false)
-- as new ones open. If a load bug ever leaves two rows is_active = true for
-- the same customer_id, fct_daily_revenue's join against active_customers
-- fans out and silently doubles (or triples) both completed_order_rows and
-- daily_revenue for that customer's orders -- with no SQL error and no
-- individual column looking wrong.
--
-- Passes when it returns zero rows.
select
    customer_id,
    count(*) as active_versions
from {{ ref('stg_customers') }}
where is_active = true
group by customer_id
having count(*) > 1
