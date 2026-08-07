{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with junior_listings as (
    select
        f.listing_id,
        f.listing_sk,
        f.title,
        f.slug,
        f.salary_min,
        f.salary_max,
        f.currency,
        f.is_gross,
        ds.level as seniority,
        det.type as employment_type,
        dwt.type as workplace_type,
        dcat.category,
        dc.company_name,
        dd.full_date as posted_date
    from {{ ref('fact_job_listings') }} f
    left join {{ ref('dim_seniority') }} ds on ds.seniority_key = f.seniority_key
    left join {{ ref('dim_employment_type') }} det on det.employment_type_key = f.employment_type_key
    left join {{ ref('dim_workplace_type') }} dwt on dwt.workplace_type_key = f.workplace_type_key
    left join {{ ref('dim_category') }} dcat on dcat.category_key = f.category_key
    left join {{ ref('dim_company') }} dc on dc.company_key = f.company_key
    left join {{ ref('dim_date') }} dd on dd.date_key = f.date_posted_key
    where ds.level = 'junior'
)

select
    jl.listing_id,
    jl.listing_sk,
    jl.title,
    jl.slug,
    jl.salary_min,
    jl.salary_max,
    jl.currency,
    jl.is_gross,
    jl.seniority,
    jl.employment_type,
    jl.workplace_type,
    jl.category,
    jl.company_name,
    jl.posted_date,
    collect_set(bt.technology_name) as technologies,
    collect_set(bc.city_name) as cities
from junior_listings jl
left join {{ ref('bridge_listing_technology') }} bt on bt.listing_id = jl.listing_id
left join {{ ref('bridge_listing_city') }} bc on bc.listing_id = jl.listing_id
group by
    jl.listing_id, jl.listing_sk, jl.title, jl.slug,
    jl.salary_min, jl.salary_max, jl.currency, jl.is_gross,
    jl.seniority, jl.employment_type, jl.workplace_type,
    jl.category, jl.company_name, jl.posted_date
