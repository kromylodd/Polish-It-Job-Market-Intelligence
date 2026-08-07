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
-- Same pattern as Silesia's snapshots_listings.sql.
-- Lets us compute listing lifetime and detect withdrawn/filled postings.

select
    listing_id,
    title,
    company_name,
    category,
    seniority,
    workplace_type,
    salary_min,
    salary_max,
    currency,
    'active' as listing_status,
    date_collected
from {{ ref('stg_listings') }}

{% endsnapshot %}
