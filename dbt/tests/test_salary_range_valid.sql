-- Custom singular test: salary_min must be <= salary_max where both are present
-- and salary_min must be > 0.
--
-- severity=warn: a handful of dirty source rows (e.g. a listing with salary_min=0)
-- should surface as a warning, not abort `dbt build` and skip every downstream
-- mart (which would silently break the daily alerts).
{{ config(severity='warn') }}

select
    listing_id,
    salary_min,
    salary_max
from {{ ref('fact_job_listings') }}
where (salary_min is not null and salary_max is not null)
  and (salary_min > salary_max or salary_min <= 0)
