-- Override dbt's default schema name generation.
-- By default, dbt creates "{target_schema}_{custom_schema}" (e.g. gold_gold).
-- We want to use the custom_schema directly (e.g. just "gold") since the DuckDB
-- schemas are created directly by the pipeline.

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
