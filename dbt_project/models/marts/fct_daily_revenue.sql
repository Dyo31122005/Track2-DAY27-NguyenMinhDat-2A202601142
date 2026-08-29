-- NOTE: If the customer dimension ever has more than one active row per
-- customer (an SCD-2 load bug), a plain join against active_customers fans
-- out and silently inflates completed_order_rows/daily_revenue -- no SQL
-- error, no individual column looks wrong. The `qualify` below is the guard:
-- it keeps the join's shape (still a LEFT JOIN, unmatched orders still kept)
-- but caps active_customers at one row per customer_id, so a duplicate
-- upstream can no longer fan out downstream. It's defense-in-depth --
-- assert_single_active_customer_version.sql (a singular test) and
-- expect_no_revenue_inflation_from_duplicate_active_customer (a unit test)
-- both guard the same failure mode from different angles.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),
active_customers as (
    select *
    from {{ ref('stg_customers') }}
    where is_active = true
    qualify row_number() over (
        partition by customer_id
        order by valid_from desc
    ) = 1
)
select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join active_customers c
    on o.customer_id = c.customer_id
group by 1
order by 1
