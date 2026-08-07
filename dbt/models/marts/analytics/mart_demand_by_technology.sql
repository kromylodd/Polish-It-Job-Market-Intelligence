{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with weekly_demand as (
    select
        bt.technology_name,
        dd.year,
        dd.week_of_year,
        dd.full_date as week_start,
        count(distinct f.listing_id) as listing_count
    from {{ ref('fact_job_listings') }} f
    inner join {{ ref('bridge_listing_technology') }} bt
        on bt.listing_id = f.listing_id
    inner join {{ ref('dim_date') }} dd
        on dd.date_key = f.date_posted_key
    group by bt.technology_name, dd.year, dd.week_of_year, dd.full_date
)

select
    technology_name,
    year,
    week_of_year,
    week_start,
    listing_count,
    lag(listing_count) over (
        partition by technology_name order by year, week_of_year
    ) as prev_week_count,
    listing_count - coalesce(
        lag(listing_count) over (
            partition by technology_name order by year, week_of_year
        ), 0
    ) as wow_change
from weekly_demand
