{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with listings as (
    select * from {{ ref('stg_listings') }}
),

listings_exploded as (
    select
        l.listing_id,
        l.listing_sk,
        l.title,
        l.slug,
        l.company_name,
        l.category,
        l.seniority,
        l.workplace_type,
        l.posted_date,
        l.date_collected,
        sv.employment_type,
        sv.salary_min,
        sv.salary_max,
        sv.currency,
        sv.is_gross
    from listings l
    lateral view explode(l.salary_variants) sv as employment_type, salary_min, salary_max, currency, is_gross
),

final as (
    select
        le.listing_id,
        le.listing_sk,
        le.title,
        le.slug,
        dc.company_key,
        ds.seniority_key,
        det.employment_type_key,
        dwt.workplace_type_key,
        dcat.category_key,
        dd_posted.date_key as date_posted_key,
        dd_collected.date_key as date_collected_key,
        le.salary_min,
        le.salary_max,
        le.currency,
        le.is_gross,
        'active' as listing_status
    from listings_exploded le
    left join {{ ref('dim_company') }} dc
        on dc.company_name = le.company_name
    left join {{ ref('dim_seniority') }} ds
        on ds.level = le.seniority
    left join {{ ref('dim_employment_type') }} det
        on det.type = le.employment_type
    left join {{ ref('dim_workplace_type') }} dwt
        on dwt.type = le.workplace_type
    left join {{ ref('dim_category') }} dcat
        on dcat.category = le.category
    left join {{ ref('dim_date') }} dd_posted
        on dd_posted.full_date = cast(le.posted_date as date)
    left join {{ ref('dim_date') }} dd_collected
        on dd_collected.full_date = cast(le.date_collected as date)
)

select * from final
