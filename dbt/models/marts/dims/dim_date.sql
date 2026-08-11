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
        end_date="current_date"
    ) }}
)

select
    {{ dbt_utils.generate_surrogate_key(['date_day']) }} as date_key,
    date_day as full_date,
    extract(year from date_day) as year,
    extract(month from date_day) as month,
    extract(day from date_day) as day,
    extract(week from date_day) as week_of_year,
    extract(dow from date_day) as day_of_week,
    case when extract(dow from date_day) in (0, 6) then true else false end as is_weekend,
    strftime(date_day, '%A') as day_name,
    strftime(date_day, '%B') as month_name,
    extract(quarter from date_day) as quarter
from date_spine
