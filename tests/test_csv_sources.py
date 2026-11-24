"""CSV ingestion from real-world exports (LinkedIn, Naukri, spreadsheets, TSV)."""
import io

import pytest

from core.csv_parser import decode_csv_bytes, detect_delimiter, parse_jobs_csv

SAMPLE_ROW = {
    "company": "Acme GmbH",
    "title": "Backend Engineer",
    "description": "Build Python services with FastAPI, PostgreSQL and Docker for our platform team.",
}


def test_semicolon_export_from_european_locale():
    csv_text = (
        "Job Title;Company Name;Job Description;Job URL;Location\n"
        f'"{SAMPLE_ROW["title"]}";"{SAMPLE_ROW["company"]}";"{SAMPLE_ROW["description"]}";'
        '"https://example.com/jobs/1";"Berlin, DE"\n'
    )
    jobs, mapping, _ = parse_jobs_csv(csv_text.encode("cp1252"))
    assert mapping["company"] == "Company Name"
    assert mapping["title"] == "Job Title"
    assert mapping["jd_text"] == "Job Description"
    assert mapping["location"] == "Location"
    assert jobs[0]["company"] == "Acme GmbH"
    assert jobs[0]["location"] == "Berlin, DE"


def test_tab_separated_and_windows_encoded_export():
    csv_text = (
        "Company\tJob Title\tJob Description\tApply Link\n"
        "Acme\tData Engineer\tDesign and maintain ETL pipelines in Python and SQL for analytics.\t"
        "https://example.com/jobs/2\n"
    )
    jobs, mapping, _ = parse_jobs_csv(csv_text.encode("cp1252"))
    assert mapping["jd_text"] == "Job Description"
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://example.com/jobs/2"


def test_linkedin_style_headers_and_bom():
    csv_text = (
        "\ufeffJob Posting Title,Hiring Organization,Description,Job URL,Location\n"
        f'{SAMPLE_ROW["title"]},{SAMPLE_ROW["company"]},"{SAMPLE_ROW["description"]}",'
        "https://example.com/jobs/3,Remote\n"
    )
    jobs, mapping, _ = parse_jobs_csv(csv_text.encode("utf-8"))
    assert mapping["company"] == "Hiring Organization"
    assert mapping["title"] == "Job Posting Title"
    assert mapping["jd_text"] == "Description"
    assert jobs[0]["title"] == "Backend Engineer"


def test_specific_headers_win_over_short_synonyms():
    csv_text = (
        "Company,Job Title,Role Description\n"
        "Acme,Engineer,Design and ship Python microservices with tests and observability.\n"
    )
    _, mapping, _ = parse_jobs_csv(csv_text)
    assert mapping["title"] == "Job Title"
    assert mapping["jd_text"] == "Role Description"


def test_missing_required_column_lists_detected_headers():
    with pytest.raises(ValueError) as error:
        parse_jobs_csv("first,second\n1,2\n")
    assert "Detected headers" in str(error.value)


def test_empty_and_short_rows_are_reported_as_warnings():
    csv_text = (
        "Company,Job Title,Job Description\n"
        "Acme,Engineer,\n"
        "Beta,Analyst,Too short.\n"
        "Gamma,Developer,\"Build and operate Python data services with SQL, Airflow and tests.\"\n"
    )
    jobs, _, warnings = parse_jobs_csv(csv_text)
    assert len(jobs) == 1
    assert len(warnings) == 2


def test_delimiter_and_decoder_helpers():
    assert detect_delimiter("a;b;c\n1;2;3\n") == ";"
    assert detect_delimiter("a\tb\tc\n") == "\t"
    assert detect_delimiter("a,b\n") == ","
    assert decode_csv_bytes("München,Germany".encode("cp1252")) == "München,Germany"
    assert decode_csv_bytes("café".encode("utf-8")) == "café"


def test_bytesio_stream_is_supported():
    stream = io.BytesIO(b"Company,Job Title,Job Description\nAcme,Engineer,"
                         b"Build Python services with SQL and automated tests for data products.\n")
    jobs, _, _ = parse_jobs_csv(stream)
    assert len(jobs) == 1
