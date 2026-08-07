{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with listing_techs as (
    select listing_id, technology_name
    from {{ ref('bridge_listing_technology') }}
    where technology_name is not null
),

pairs as (
    select
        a.technology_name as tech_a,
        b.technology_name as tech_b,
        a.listing_id
    from listing_techs a
    inner join listing_techs b
        on a.listing_id = b.listing_id
        and a.technology_name < b.technology_name
)

select
    tech_a,
    tech_b,
    count(distinct listing_id) as co_occurrence_count
from pairs
group by tech_a, tech_b
having count(distinct listing_id) >= 3
order by co_occurrence_count desc
