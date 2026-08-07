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
--
-- listing_status is derived from expiry_date so the SCD2 actually captures a
-- lifecycle transition (active -> expired). Previously it was a hardcoded
-- 'active', which meant the status check_col could never change.
-- salary_min/max/currency are derived from the salary_variants array, since
-- stg_listings stores salary as an array of variants, not scalar columns.

select
    listing_id,
    title,
    company_name,
    category,
    seniority,
    workplace_type,
    array_min(transform(salary_variants, v -> v.salary_min)) as salary_min,
    array_max(transform(salary_variants, v -> v.salary_max)) as salary_max,
    element_at(salary_variants, 1).currency as currency,
    case
        when try_cast(expiry_date as timestamp) is not null
             and try_cast(expiry_date as timestamp) < current_timestamp()
        then 'expired'
        else 'active'
    end as listing_status,
    date_collected
from {{ ref('stg_listings') }}

{% endsnapshot %}
