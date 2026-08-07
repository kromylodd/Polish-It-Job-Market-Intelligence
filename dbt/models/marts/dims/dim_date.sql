{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with date_spine as (
    {{ dbt_utils.date_spine(
        datepart="day",
        start_date="cast('2026-01-01' as date)",
        end_date="current_date()"
    ) }}
)

select
    {{ dbt_utils.generate_surrogate_key(['date_day']) }} as date_key,
    date_day as full_date,
    year(date_day) as year,
    month(date_day) as month,
    day(date_day) as day,
    weekofyear(date_day) as week_of_year,
    dayofweek(date_day) as day_of_week,
    case when dayofweek(date_day) in (1, 7) then true else false end as is_weekend,
    date_format(date_day, 'EEEE') as day_name,
    date_format(date_day, 'MMMM') as month_name,
    quarter(date_day) as quarter
from date_spine
