{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with companies as (
    select distinct company_name
    from {{ ref('stg_listings') }}
    where company_name is not null and company_name != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['company_name']) }} as company_key,
    company_name
from companies
