{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with listing_cities as (
    select
        listing_id,
        unnest(cities) as city_name
    from {{ ref('stg_listings') }}
    where cities is not null
      and len(cities) > 0
),

final as (
    select
        lc.listing_id,
        dc.city_key,
        lc.city_name
    from listing_cities lc
    left join {{ ref('dim_city') }} dc
        on dc.city_name = lc.city_name
)

select * from final
