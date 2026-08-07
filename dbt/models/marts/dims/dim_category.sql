{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with categories as (
    select distinct category
    from {{ ref('stg_listings') }}
    where category is not null and category != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['category']) }} as category_key,
    category
from categories
