-- Custom singular test: salary_min must be <= salary_max where both are present
-- and salary_min must be > 0

select
    listing_id,
    salary_min,
    salary_max
from {{ ref('fact_job_listings') }}
where (salary_min is not null and salary_max is not null)
  and (salary_min > salary_max or salary_min <= 0)
