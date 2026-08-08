"""
Shared filter logic for Polish IT Job Market Intelligence.

Provides a universal filter system with tolerance (fuzzy) matching:
- A listing passes if at most `tolerance` filter dimensions mismatch.
- tolerance=0 → strict (all filters must match)
- tolerance=1 → one filter can be "wrong" and listing still appears
- tolerance=2 → two filters can mismatch, etc.

Filter dimensions:
- seniority: junior, mid, senior, lead, manager
- technologies: any tech/skill names
- categories: job categories (python, java, devops, data, etc.)
- workplace_type: remote, hybrid, office
- employment_type: b2b, permanent (uop), mandate_contract (uz), any
- salary_min: minimum acceptable salary (listings below this don't match)
- cities: location filter
"""

from typing import Any

# --- All known values for each dimension ---

ALL_CATEGORIES = {
    "python": "🐍 Python",
    "java": "☕ Java",
    "javascript": "🟨 JavaScript",
    "net": "🟣 .NET/C#",
    "c": "⚙️ C/C++",
    "go": "🔵 Go",
    "php": "🐘 PHP",
    "html": "🌐 Frontend/HTML",
    "mobile": "📱 Mobile",
    "data": "📊 Data",
    "ai": "🤖 AI/ML",
    "devops": "🔧 DevOps",
    "admin": "🖥️ Admin/IT",
    "security": "🔒 Security",
    "testing": "🧪 Testing/QA",
    "pm": "📋 Project Management",
    "analytics": "📈 Analytics/BI",
    "architecture": "🏗️ Architecture",
    "erp": "🏢 ERP/SAP",
    "support": "🆘 Support",
    "other": "❓ Other",
}

ALL_SENIORITIES = {
    "junior": "🌱 Junior",
    "mid": "💼 Mid",
    "senior": "🏆 Senior",
    "lead": "👑 Lead",
    "manager": "📊 Manager",
}

ALL_WORKPLACES = {
    "remote": "🏠 Remote",
    "hybrid": "🔀 Hybrid",
    "office": "🏢 Office",
}

ALL_EMPLOYMENT_TYPES = {
    "b2b": "📄 B2B",
    "permanent": "📋 UoP (permanent)",
    "mandate_contract": "📝 Umowa zlecenie",
    "any": "🤷 Any/unspecified",
}

# Technologies grouped by domain (for display and suggestions)
TECH_CATEGORIES = {
    "🐍 Languages": [
        "Python",
        "Java",
        "JavaScript",
        "TypeScript",
        "C#",
        "C++",
        "Go",
        "Rust",
        "Kotlin",
        "Swift",
        "PHP",
        "Ruby",
        "Scala",
        "R",
    ],
    "🌐 Frontend": [
        "React",
        "Angular",
        "Vue.js",
        "Next.js",
        "HTML",
        "CSS",
        "Tailwind",
        "Svelte",
    ],
    "⚙️ Backend": [
        "Node.js",
        "Spring",
        "Django",
        "FastAPI",
        "Flask",
        ".NET",
        "Express",
        "NestJS",
        "Laravel",
    ],
    "📊 Data & Analytics": [
        "SQL",
        "Apache Spark",
        "Apache Kafka",
        "Apache Airflow",
        "dbt",
        "Pandas",
        "PySpark",
        "Snowflake",
        "Databricks",
        "Power BI",
        "Tableau",
    ],
    "☁️ Cloud & DevOps": [
        "AWS",
        "Azure",
        "GCP",
        "Docker",
        "Kubernetes",
        "Terraform",
        "Ansible",
        "Jenkins",
        "GitLab CI",
        "GitHub Actions",
        "Linux",
    ],
    "🗄️ Databases": [
        "PostgreSQL",
        "MySQL",
        "MongoDB",
        "Redis",
        "Elasticsearch",
        "Oracle",
        "MS SQL",
        "Cassandra",
        "DynamoDB",
    ],
    "🤖 ML & AI": [
        "TensorFlow",
        "PyTorch",
        "MLflow",
        "scikit-learn",
        "LLM",
        "OpenAI",
        "Hugging Face",
        "NLP",
        "Computer Vision",
    ],
    "📱 Mobile": [
        "React Native",
        "Flutter",
        "iOS",
        "Android",
        "SwiftUI",
        "Jetpack Compose",
    ],
    "🧪 Testing": [
        "Selenium",
        "Cypress",
        "Jest",
        "Playwright",
        "JUnit",
        "pytest",
    ],
    "🔒 Security": [
        "OWASP",
        "Penetration Testing",
        "SIEM",
        "SOC",
        "Firewall",
        "IAM",
    ],
}

ALL_KNOWN_TECHS = [tech for techs in TECH_CATEGORIES.values() for tech in techs]

# Known cities from justjoin.it (Polish + common cross-border)
KNOWN_CITIES = {
    "Białystok",
    "Bielsko-Biała",
    "Bydgoszcz",
    "Częstochowa",
    "Gdańsk",
    "Gdynia",
    "Gliwice",
    "Gorzów Wielkopolski",
    "Katowice",
    "Kielce",
    "Kraków",
    "Lublin",
    "Łódź",
    "Nowy Sącz",
    "Olsztyn",
    "Opole",
    "Poznań",
    "Płock",
    "Radom",
    "Rzeszów",
    "Rybnik",
    "Sopot",
    "Sosnowiec",
    "Szczecin",
    "Toruń",
    "Trójmiasto",
    "Warszawa",
    "Warsaw",
    "Wrocław",
    "Zabrze",
    "Zielona Góra",
    # Common remote/international
    "Remote",
    "Polska",
}

# --- Default user config ---

DEFAULT_USER_CONFIG = {
    "seniorities": ["junior", "mid", "senior", "lead", "manager"],
    "technologies": [],  # Empty = don't filter on tech (show all)
    "categories": list(ALL_CATEGORIES.keys()),  # All enabled by default
    "workplace_types": ["remote", "hybrid", "office"],  # All enabled
    "employment_types": ["b2b", "permanent", "mandate_contract", "any"],  # All enabled
    "salary_min": 0,  # 0 = no minimum
    "cities": [],  # Empty = any location
    "tolerance": 1,  # How many dimensions can mismatch
}


def match_listing(listing: dict[str, Any], config: dict) -> tuple[bool, int]:
    """Check if a listing matches the user's filters with tolerance.

    Returns (matches: bool, mismatches: int).
    A listing matches if mismatches <= tolerance.
    Only active filters (non-empty) count as dimensions.
    """
    tolerance = config.get("tolerance", 1)
    mismatches = 0

    # --- Seniority ---
    seniorities = config.get("seniorities", [])
    if seniorities:
        listing_seniority = (listing.get("seniority") or "").lower()
        if listing_seniority not in seniorities:
            mismatches += 1

    # --- Technologies ---
    technologies = config.get("technologies", [])
    if technologies:
        listing_techs = _get_listing_techs(listing)
        # Case-insensitive match
        listing_techs_lower = {t.lower() for t in listing_techs}
        wanted_lower = {t.lower() for t in technologies}
        if not listing_techs_lower & wanted_lower:
            mismatches += 1

    # --- Category ---
    categories = config.get("categories", [])
    if categories:
        listing_category = (listing.get("category") or "").lower()
        if listing_category not in [c.lower() for c in categories]:
            mismatches += 1

    # --- Workplace type ---
    workplace_types = config.get("workplace_types", [])
    if workplace_types:
        listing_workplace = (listing.get("workplace_type") or "").lower()
        if listing_workplace not in [w.lower() for w in workplace_types]:
            mismatches += 1

    # --- Employment type ---
    employment_types = config.get("employment_types", [])
    if employment_types:
        listing_emp_types = _get_listing_employment_types(listing)
        listing_emp_lower = {e.lower() for e in listing_emp_types}
        wanted_emp_lower = {e.lower() for e in employment_types}
        if not listing_emp_lower & wanted_emp_lower:
            mismatches += 1

    # --- Salary minimum ---
    salary_min = config.get("salary_min", 0)
    if salary_min and salary_min > 0:
        listing_salary_max = _get_listing_max_salary(listing)
        if listing_salary_max is not None and listing_salary_max < salary_min:
            mismatches += 1
        # If salary is undisclosed, don't count as mismatch (benefit of doubt)

    # --- Cities ---
    cities = config.get("cities", [])
    if cities:
        listing_cities = listing.get("cities", [])
        if isinstance(listing_cities, str):
            listing_cities = [listing_cities] if listing_cities else []
        listing_cities_lower = {c.lower() for c in listing_cities}
        wanted_cities_lower = {c.lower() for c in cities}
        if not listing_cities_lower & wanted_cities_lower:
            mismatches += 1

    return (mismatches <= tolerance, mismatches)


def _get_listing_techs(listing: dict) -> set[str]:
    """Extract all technology/skill names from a listing."""
    techs = set()

    # From parsed schema
    for key in ("technologies", "required_skills", "nice_to_have_skills"):
        val = listing.get(key, [])
        if isinstance(val, list):
            techs.update(str(t) for t in val if t)
        elif isinstance(val, str) and val:
            techs.add(val)

    return techs


def _get_listing_employment_types(listing: dict) -> set[str]:
    """Extract employment types from a listing."""
    types = set()

    # From parsed schema (salary_variants array)
    variants = listing.get("salary_variants", [])
    if isinstance(variants, list):
        for v in variants:
            if isinstance(v, dict) and v.get("employment_type"):
                types.add(v["employment_type"])

    # Direct field (gold mart schema)
    emp_type = listing.get("employment_type")
    if emp_type:
        types.add(emp_type)

    return types


def _get_listing_max_salary(listing: dict) -> int | None:
    """Get the maximum salary from a listing (for salary_min filter)."""
    # Gold mart schema
    if listing.get("salary_max") is not None:
        try:
            return int(listing["salary_max"])
        except (ValueError, TypeError):
            pass

    # Parsed schema (salary_variants)
    variants = listing.get("salary_variants", [])
    if isinstance(variants, list):
        maxes = []
        for v in variants:
            if isinstance(v, dict) and v.get("salary_max") is not None:
                try:
                    maxes.append(int(v["salary_max"]))
                except (ValueError, TypeError):
                    pass
        if maxes:
            return max(maxes)

    return None


def filter_listings(listings: list[dict[str, Any]], config: dict) -> list[dict[str, Any]]:
    """Filter a list of listings using match_listing. Returns matched listings."""
    results = []
    for listing in listings:
        matches, _ = match_listing(listing, config)
        if matches:
            results.append(listing)
    return results
