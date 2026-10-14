"""A job added from a link or pasted text (`apply/intake.py`).

Structured data is read as it is; a page without it is cut by the model's
quoted boundaries; a Ukrainian posting keeps its own words for the analyzer
but gets an English title, employer and place, and a letter in English.
"""

from __future__ import annotations

import json

import pytest

from pitwall.apply import intake

UK_BODY = (
    "Шукаємо бізнес-аналітика до команди, яка розвиває цифрові продукти для потреб "
    "оборонного сектору. Що потрібно робити: проводити бізнес-аналіз, документувати "
    "вимоги, проєктувати інтеграції між системами, писати технічні специфікації та "
    "брати участь у тестуванні. Вимоги: досвід від 2 років, розуміння SDLC, REST API, "
    "SQL. Буде плюсом: SAP S/4HANA або Odoo. Що ми пропонуємо: роботу в Києві, офіс, "
    "участь у проєктах цифровізації державного сектору."
)


class StubRouter:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def complete_json(self, task, system, user, **kw):
        self.calls.append(task)
        return dict(self.answer)


def _answer(**over) -> dict:
    base = {
        "title": "Бізнес-аналітик", "company": "Міністерство оборони України",
        "location": "Київ", "title_en": "Business Analyst",
        "company_en": "Ministry of Defence of Ukraine", "location_en": "Kyiv",
        "language": "uk", "starts_with": "Шукаємо бізнес-аналітика до команди, яка розвиває",
        "ends_with": "участь у проєктах цифровізації державного сектору.",
        "is_job_posting": True,
    }
    return base | over


def test_structured_data_is_read_as_it_is():
    ld = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, {
        "@type": "JobPosting", "title": "Data Scientist",
        "hiringOrganization": {"@type": "Organization", "name": "Uvik Software"},
        "jobLocation": [{"@type": "Place", "address": {
            "addressLocality": "Lisbon", "addressCountry": "PT"}}],
        "description": "<p>We are looking for a Data Scientist.</p>" + "<li>Python</li>" * 80,
        "datePosted": "2026-10-01T09:00:00",
    }]}
    page = (f"<html><head><script type='application/ld+json'>{json.dumps(ld)}</script>"
            "</head><body>menu</body></html>")
    router = StubRouter(_answer())
    p = intake.from_html(page, "https://x.test/jobs/1", router=router)
    assert (p.method, p.title, p.company, p.location) == (
        "structured data", "Data Scientist", "Uvik Software", "Lisbon, PT")
    assert "• Python" in p.description and p.language == "en"
    assert router.calls == [], "nothing was missing, so no model call"


def test_a_page_without_structured_data_is_cut_out_of_its_chrome():
    page = ("<html><head><title>Бізнес-аналітик в Міністерство оборони України | DOU</title>"
            "</head><body><nav>Форум Стрічка Зарплати Робота</nav><h1>Бізнес-аналітик</h1>"
            f"<div>{UK_BODY}</div><a>Відгукнутися на вакансію</a><footer>Гарячі вакансії"
            "</footer></body></html>")
    p = intake.from_html(page, "https://jobs.dou.ua/x/", router=StubRouter(_answer()))
    assert p.method == "page text"
    assert p.description.startswith("Шукаємо") and p.description.endswith("сектору.")
    assert "Форум" not in p.description and "Відгукнутися" not in p.description
    # The engine reads the employer's words; the letter gets English names.
    assert (p.title, p.company, p.location) == (
        "Business Analyst", "Ministry of Defence of Ukraine", "Kyiv")
    assert p.original["title"] == "Бізнес-аналітик" and p.language == "uk"


def test_the_cut_ignores_punctuation_spacing_and_a_title_seen_twice():
    text = ("Data Analyst | Board\nMenu\nData Analyst at Acme\n" + "x " * 200
            + "\nYou will love it here .\nApply now\nOther jobs: Data Analyst at Acme")
    body, ok = intake.cut(text, "Data Analyst at Acme", "You will love it here.")
    assert ok and body.startswith("Data Analyst at Acme") and body.endswith("here .")


def test_the_whole_text_is_kept_when_the_boundaries_are_not_found():
    text = "word " * 100
    body, ok = intake.cut(text, "not on the page at all", "nor is this one here")
    assert body == text and not ok


def test_pasted_text_works_without_a_link():
    p = intake.from_text(f"Бізнес-аналітик\nКиїв\n\n{UK_BODY}\n\nВідгукнутися",
                         router=StubRouter(_answer()))
    assert p.method == "pasted text" and p.title == "Business Analyst" and not p.missing


def test_too_little_text_says_so():
    with pytest.raises(intake.IntakeError, match="too short"):
        intake.from_text("Data Scientist at Acme", router=StubRouter(_answer()))


def test_linkedin_is_never_fetched():
    def fetcher(url):
        raise AssertionError("fetched")

    with pytest.raises(intake.IntakeError, match="paste"):
        intake.fetch_posting("https://www.linkedin.com/jobs/view/1", fetcher=fetcher)


def test_a_refusal_becomes_a_reason_to_paste():
    from pitwall.sources.jobs.base import SourceError

    def fetcher(url):
        raise SourceError(f"{url}: refused (403). The site declines this User-Agent")

    with pytest.raises(intake.IntakeError, match=r"work\.ua .*403.*Paste"):
        intake.fetch_posting("https://www.work.ua/jobs/1/", fetcher=fetcher)


def test_a_model_failure_leaves_editable_fields_and_a_note():
    class Broken:
        def complete_json(self, *a, **kw):
            raise RuntimeError("rate limited")

    p = intake.from_text(UK_BODY + " " + UK_BODY, router=Broken())
    assert p.notes and "fill in" in p.notes[0]
    assert "title" in p.missing


@pytest.mark.parametrize(("place", "english"), [
    ("Дніпро, віддалено", "Dnipro, Remote"), ("Біла Церква", "Bila Tserkva"),
    ("Zürich", "Zürich"), ("Kyiv", "Kyiv"),
])
def test_places_come_out_in_english(place, english):
    assert intake.english_place(place) == english


def test_the_language_guess():
    assert intake.guess_language(UK_BODY) == "uk"
    assert intake.guess_language("Мы ищем аналитика. Это работа в офисе, объём задач большой.") == "ru"
    assert intake.guess_language("We are hiring and you will work with the team") == "en"


def test_an_added_job_is_selected_and_its_letter_is_in_english(repos):
    from pitwall.apply.jobs import load_job
    from pitwall.jobsearch import tracker

    p = intake.from_text(UK_BODY, "https://jobs.dou.ua/x/", router=StubRouter(_answer()))
    job_id = intake.add_posting(repos.conn, p)
    again = intake.add_posting(repos.conn, p)
    assert again == job_id, "the same link twice is the same job"
    job = load_job(job_id, repos.conn)
    assert job["title"] == "Business Analyst" and job["posting_company"].startswith("Ministry")
    assert job["description"].startswith("Шукаємо")
    assert job["letter_language"] == "en"
    app = tracker.for_cluster(repos.conn, int(tracker.cluster_for_job(repos.conn, job_id)
                                              ["cluster_id"]))
    assert app["status"] == "SHORTLISTED"


def test_a_job_the_search_found_leaves_the_language_to_the_planner(repos):
    from pitwall.apply.jobs import load_job

    repos.conn.execute("INSERT INTO jobs (title, url, description, raw_json) "
                       "VALUES ('DS', 'https://a.test', 'text', '{\"id\": 1}')")
    job_id = repos.conn.execute("SELECT id FROM jobs WHERE url = 'https://a.test'").fetchone()[0]
    assert "letter_language" not in load_job(job_id, repos.conn)


def test_missing_fields_refuse_to_save(repos):
    p = intake.Posting(title="", company="Acme", description="x" * 400)
    with pytest.raises(intake.IntakeError, match="title"):
        intake.add_posting(repos.conn, p)
