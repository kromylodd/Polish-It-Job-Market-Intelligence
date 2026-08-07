-- Snapshot integrity: no listing should have more than one active row
-- (dbt_valid_to IS NULL means currently active)
-- Reuse of the same check from Silesia's snapshots

select
    listing_id,
    count(*) as active_row_count
from {{ ref('snapshot_listings') }}
where dbt_valid_to is null
group by listing_id
having count(*) > 1
