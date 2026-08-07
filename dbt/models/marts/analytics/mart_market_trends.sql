{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with daily_stats as (
    select
        dd.full_date,
        dd.week_of_year,
        dd.month,
        dd.year,
        count(distinct f.listing_id) as new_listings,
        round(avg((f.salary_min + f.salary_max) / 2), 0) as avg_salary_mid
    from {{ ref('fact_job_listings') }} f
    inner join {{ ref('dim_date') }} dd
        on dd.date_key = f.date_posted_key
    where f.salary_min is not null and f.salary_min > 0
    group by dd.full_date, dd.week_of_year, dd.month, dd.year
)

select
    full_date,
    week_of_year,
    month,
    year,
    new_listings,
    avg_salary_mid,
    sum(new_listings) over (
        order by full_date rows between 6 preceding and current row
    ) as rolling_7d_listings,
    avg(avg_salary_mid) over (
        order by full_date rows between 6 preceding and current row
    ) as rolling_7d_avg_salary
from daily_stats
order by full_date
