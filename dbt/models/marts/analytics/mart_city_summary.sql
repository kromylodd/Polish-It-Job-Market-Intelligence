{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with city_listings as (
    select
        bc.city_name,
        f.listing_id,
        -- monthly-normalized (see fact_job_listings) so per-hour B2B rates
        -- don't drag city averages down
        f.salary_min_monthly as salary_min,
        f.salary_max_monthly as salary_max,
        f.currency
    from {{ ref('fact_job_listings') }} f
    inner join {{ ref('bridge_listing_city') }} bc
        on bc.listing_id = f.listing_id
)

select
    city_name,
    count(distinct listing_id) as listing_count,
    round(avg(salary_min), 0) as avg_salary_min,
    round(avg(salary_max), 0) as avg_salary_max,
    round(avg((salary_min + salary_max) / 2), 0) as avg_salary_mid
from city_listings
where salary_min is not null and salary_min > 0
group by city_name
order by listing_count desc
