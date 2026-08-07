{{
    config(
        materialized='view',
        schema='silver'
    )
}}

with source as (
    select * from {{ source('silver', 'listings_with_tech') }}
),

final as (
    select
        {{ dbt_utils.generate_surrogate_key(['listing_id', 'date_collected']) }} as listing_sk,
        listing_id,
        slug,
        title,
        apply_url,
        company_name,
        category,
        seniority,
        workplace_type,
        cities,
        canonical_required_skills as required_skills,
        canonical_nice_to_have_skills as nice_to_have_skills,
        all_technologies,
        salary_variants,
        description,
        posted_date,
        expiry_date,
        date_collected,
        source_run_id,
        ingested_at,
        silver_loaded_at,
        tech_parsed_at
    from source
    where listing_id is not null
      and title is not null
      and title != ''
      and company_name is not null
      and company_name != ''
)

select * from final
