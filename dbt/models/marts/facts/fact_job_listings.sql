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
        sv.col.employment_type,
        sv.col.salary_min,
        sv.col.salary_max,
        sv.col.currency,
        sv.col.is_gross,
        sv.col.unit as pay_unit,
        -- Monthly-normalization factor. justjoin.it quotes B2B/mandate pay per
        -- hour (or, rarely, per day/week/year) in the same salary field as
        -- monthly permanent (UoP) pay, carrying the period only in `unit`.
        -- Convert everything to a monthly basis so the gold marts aggregate
        -- comparable figures. ~168 working hours / ~21 working days per month.
        case lower(coalesce(sv.col.unit, 'month'))
            when 'hour' then 168.0
            when 'hourly' then 168.0
            when 'day' then 21.0
            when 'daily' then 21.0
            when 'week' then 4.33
            when 'weekly' then 4.33
            when 'year' then 1.0 / 12.0
            when 'annually' then 1.0 / 12.0
            when 'annum' then 1.0 / 12.0
            else 1.0
        end as pay_factor
    from listings l
    lateral view explode(l.salary_variants) sv
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
        le.pay_unit,
        -- Period-normalized monthly salary. The gold marts aggregate these
        -- (not the raw min/max) so hourly B2B rates no longer contaminate
        -- monthly medians. Raw salary_min/max + pay_unit are kept for lineage.
        round(le.salary_min * le.pay_factor, 0) as salary_min_monthly,
        round(le.salary_max * le.pay_factor, 0) as salary_max_monthly,
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
