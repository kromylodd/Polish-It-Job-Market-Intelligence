{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with fact_with_tech as (
    select
        f.listing_id,
        -- Period-normalized monthly figures so hourly B2B rates don't get
        -- blended with monthly UoP pay (see fact_job_listings normalization).
        f.salary_min_monthly as salary_min,
        f.salary_max_monthly as salary_max,
        f.currency,
        f.is_gross,
        ds.level as seniority,
        det.type as employment_type,
        bt.technology_name
    from {{ ref('fact_job_listings') }} f
    inner join {{ ref('bridge_listing_technology') }} bt
        on bt.listing_id = f.listing_id
    left join {{ ref('dim_seniority') }} ds
        on ds.seniority_key = f.seniority_key
    left join {{ ref('dim_employment_type') }} det
        on det.employment_type_key = f.employment_type_key
    where f.salary_min_monthly is not null
      and f.salary_min_monthly > 0
      and f.salary_max_monthly >= f.salary_min_monthly
)

select
    technology_name,
    seniority,
    employment_type,
    currency,
    count(*) as listing_count,
    round(avg(salary_min), 0) as avg_salary_min,
    round(avg(salary_max), 0) as avg_salary_max,
    round(avg((salary_min + salary_max) / 2), 0) as avg_salary_mid,
    round(percentile_approx((salary_min + salary_max) / 2, 0.5), 0) as median_salary,
    round(percentile_approx((salary_min + salary_max) / 2, 0.25), 0) as p25_salary,
    round(percentile_approx((salary_min + salary_max) / 2, 0.75), 0) as p75_salary,
    min(salary_min) as min_salary,
    max(salary_max) as max_salary
from fact_with_tech
group by technology_name, seniority, employment_type, currency
