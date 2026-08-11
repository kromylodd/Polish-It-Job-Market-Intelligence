{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with listings as (
    select * from {{ ref('stg_listings') }}
),

-- salary_variants is stored as a JSON string (array of objects).
-- Unnest using DuckDB's json_array_length + range pattern.
listings_with_indices as (
    select
        l.*,
        unnest(range(0, json_array_length(l.salary_variants)::int)) as sv_idx
    from listings l
    where l.salary_variants is not null
      and l.salary_variants != '[]'
      and l.salary_variants != ''
      and json_valid(l.salary_variants)
      and json_array_length(l.salary_variants) > 0
),

listings_exploded as (
    select
        li.listing_id,
        li.listing_sk,
        li.title,
        li.slug,
        li.company_name,
        li.category,
        li.seniority,
        li.workplace_type,
        li.posted_date,
        li.date_collected,
        json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].employment_type') as employment_type,
        try_cast(json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].salary_min') as double) as salary_min,
        try_cast(json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].salary_max') as double) as salary_max,
        json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].currency') as currency,
        json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].is_gross') = 'true' as is_gross,
        json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].unit') as pay_unit,
        -- Monthly-normalization factor. justjoin.it quotes B2B/mandate pay per
        -- hour (or, rarely, per day/week/year) in the same salary field as
        -- monthly permanent (UoP) pay, carrying the period only in `unit`.
        -- Convert everything to a monthly basis so the gold marts aggregate
        -- comparable figures. ~168 working hours / ~21 working days per month.
        case lower(coalesce(json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].unit'), 'month'))
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
    from listings_with_indices li
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
        on dd_posted.full_date = try_cast(le.posted_date as date)
    left join {{ ref('dim_date') }} dd_collected
        on dd_collected.full_date = try_cast(le.date_collected as date)
)

select * from final
