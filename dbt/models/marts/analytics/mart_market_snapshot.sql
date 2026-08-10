{{
    config(
        materialized='table',
        schema='gold'
    )
}}

-- All-seniorities snapshot of active listings, one row per listing with its
-- technologies and cities collected into arrays. Backs the daily Telegram
-- alerts (per-user filters — including seniority — are applied downstream) and
-- the /company + /latest serving-cache reads, so users filtering for
-- senior/mid get matches, not just junior. mart_junior_market_snapshot is a
-- junior-only view over this table, kept for backward compatibility.

with listings as (
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
)

select
    l.listing_id,
    l.listing_sk,
    l.title,
    l.slug,
    l.salary_min,
    l.salary_max,
    l.currency,
    l.is_gross,
    l.seniority,
    l.employment_type,
    l.workplace_type,
    l.category,
    l.company_name,
    l.posted_date,
    collect_set(bt.technology_name) as technologies,
    collect_set(bc.city_name) as cities
from listings l
left join {{ ref('bridge_listing_technology') }} bt on bt.listing_id = l.listing_id
left join {{ ref('bridge_listing_city') }} bc on bc.listing_id = l.listing_id
group by
    l.listing_id, l.listing_sk, l.title, l.slug,
    l.salary_min, l.salary_max, l.currency, l.is_gross,
    l.seniority, l.employment_type, l.workplace_type,
    l.category, l.company_name, l.posted_date
