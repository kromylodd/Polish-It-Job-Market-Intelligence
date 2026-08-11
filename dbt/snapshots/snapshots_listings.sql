{% snapshot snapshot_listings %}

{{
    config(
        target_schema='gold',
        unique_key='listing_id',
        strategy='check',
        check_cols=['listing_status', 'salary_min', 'salary_max'],
    )
}}

-- SCD2 on listing_status and salary changes.
-- Lets us compute listing lifetime and detect withdrawn/filled postings.
--
-- listing_status is derived from expiry_date so the SCD2 actually captures a
-- lifecycle transition (active -> expired). salary_min/max are extracted from
-- the JSON salary_variants array.
-- Note: DuckDB json_extract returns values from the first element for simplicity.

select
    listing_id,
    title,
    company_name,
    category,
    seniority,
    workplace_type,
    try_cast(json_extract_string(salary_variants, '$[0].salary_min') as double) as salary_min,
    try_cast(json_extract_string(salary_variants, '$[0].salary_max') as double) as salary_max,
    json_extract_string(salary_variants, '$[0].currency') as currency,
    case
        when try_cast(expiry_date as timestamp) is not null
             and try_cast(expiry_date as timestamp) < current_timestamp
        then 'expired'
        else 'active'
    end as listing_status,
    date_collected
from {{ ref('stg_listings') }}

{% endsnapshot %}
