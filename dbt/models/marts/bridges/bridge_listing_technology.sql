{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with required_skills as (
    select
        listing_id,
        explode(required_skills) as technology_name,
        'required' as requirement_type
    from {{ ref('stg_listings') }}
    where required_skills is not null
),

nice_to_have_skills as (
    select
        listing_id,
        explode(nice_to_have_skills) as technology_name,
        'nice_to_have' as requirement_type
    from {{ ref('stg_listings') }}
    where nice_to_have_skills is not null
),

all_skills as (
    select * from required_skills
    union all
    select * from nice_to_have_skills
),

final as (
    select
        a.listing_id,
        dt.technology_key,
        a.technology_name,
        a.requirement_type
    from all_skills a
    left join {{ ref('dim_technology') }} dt
        on dt.canonical_name = a.technology_name
)

select * from final
