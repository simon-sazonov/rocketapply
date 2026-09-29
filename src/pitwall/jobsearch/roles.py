"""What kind of job a posting is — §13 of the spec.

Exact title matching is not enough ("Decision Intelligence Scientist" is highly
relevant and matches no list), and neither is keyword matching (the live
Arbeitnow feed has "AI Deployment Strategist" and "Affiliate Manager (AI
Product)"). So a label needs two things:

1. a **domain** word — machine learning, data science, optimisation, …
2. a **technical role** noun — engineer, scientist, researcher, developer, …

"AI Engineer" has both. "AI Deployment Strategist" has a domain word and a
business role noun, and comes back OTHER. Several labels can apply at once
(§13: an ML Engineer is MACHINE_LEARNING, SOFTWARE_ENGINEERING and AI).

For a generic title — "Engineer", "Scientist", "Member of Technical Staff",
"Researcher" — the description decides, but only on repeated evidence, and the
confidence says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LABELS = (
    "MACHINE_LEARNING", "AI", "DATA_SCIENCE", "APPLIED_SCIENCE", "DECISION_SCIENCE",
    "OPTIMIZATION", "SIMULATION", "DATA_ENGINEERING", "SOFTWARE_ENGINEERING",
    "ANALYTICS", "BI", "OTHER",
)

#: label -> patterns that name the domain in a TITLE. Most carry their own role
#: noun ("data scientist"); those that do not ("machine learning") need one.
#: Everything is case-insensitive except the `(?-i:…)` abbreviations: "AI",
#: "ML", "BI" and "KI" are also ordinary letters in other words and languages.
_TITLE_DOMAIN: dict[str, tuple[str, ...]] = {
    "MACHINE_LEARNING": (
        r"machine[- ]learning", r"\b(?-i:ML)\b", r"\bmlops\b", r"deep[- ]learning",
        r"computer vision", r"\bnlp\b", r"\bllms?\b", r"perception", r"reinforcement learning",
        r"maschinelles lernen",
    ),
    "AI": (
        r"\b(?-i:AI)\b", r"\b(?-i:A\.I\.)", r"artificial intelligence", r"\bgen ?ai\b",
        # "Ai Engineer", "Principal Ai Engineer", "Ai Integration Engineer": title
        # case from Jooble and Adzuna US. Only right before a role noun, since
        # "ai" is also a word in French and Italian.
        r"\b(?-i:Ai)\b(?=[\s/-]+(?:[A-Za-z]+\s+)?(?:engineer|scientist|researcher|developer|"
        r"architect|specialist))",
        r"\b(?-i:KI)\b", r"künstliche intelligenz", r"\b(?-i:IA)\b",
        r"intelligence artificielle", r"\bllms?\b",
    ),
    "DATA_SCIENCE": (
        r"data scien", r"datenwissenschaft", r"statistician", r"\bstatistik",
        r"quantitative (?:researcher|developer|modeller|modeler)", r"\bquant\b",
        r"science des données",
    ),
    "APPLIED_SCIENCE": (
        r"applied scien", r"research scien", r"research engineer", r"computational scien",
        r"member of (?:the )?technical staff", r"\bmts\b", r"scientific (?:software|programmer)",
    ),
    "DECISION_SCIENCE": (r"decision scien", r"decision intelligence", r"decision analy",
                         r"decision support"),
    "OPTIMIZATION": (r"optimi[sz]ation", r"operations research", r"operational research",
                     r"\b(?-i:OR) (?:scientist|engineer|analyst)", r"\bsolver",
                     r"planning scien"),
    "SIMULATION": (r"simulation", r"modell?ing engineer", r"digital twin", r"\bsim\b",
                   r"strategy engineer", r"vehicle dynamics"),
    "DATA_ENGINEERING": (r"data engineer", r"analytics engineer", r"data platform",
                         r"\betl\b", r"data infrastructure", r"dateningenieur",
                         r"\bdata ?ops\b"),
    "SOFTWARE_ENGINEERING": (r"software", r"developer", r"\bswe\b", r"back[- ]?end",
                             r"full[- ]?stack", r"programmer", r"entwickler",
                             r"développeur", r"platform engineer", r"infrastructure engineer"),
    "ANALYTICS": (r"data analy", r"analytics", r"product analyst", r"insights? analyst",
                  r"datenanaly", r"business analyst", r"quantitative analyst",
                  r"performance analyst"),
    "BI": (r"business intelligence", r"\b(?-i:BI)\b", r"power ?bi", r"tableau",
           r"reporting analyst", r"reporting specialist"),
}

#: A technical role noun. Without one, a domain word in the title does not make
#: the job technical ("AI Deployment Strategist").
_TECH_ROLE = re.compile(
    r"\b(?:engineer\w*|scientist\w*|research\w*|developer\w*|programmer\w*|architect\w*|"
    r"modell?er\w*|analyst\w*|specialist\w*|technologist|mts|member of (?:the )?technical staff|"
    r"ingenieur\w*|ingénieur\w*|entwickler\w*|wissenschaftler\w*|chercheur\w*|"
    r"développeur\w*|data scien\w*|statistician|quant)\b",
    re.I,
)
#: Early-career role nouns. They make a domain word technical ("Machine
#: Learning Intern") but lose to a business word ("Investment Banking Intern
#: (AI Evaluation)") — an intern's discipline is whatever else the title says.
_TRAINEE_ROLE = re.compile(
    r"\b(?:intern\w*|werkstudent\w*|working student|praktikant\w*|praktikum|stagiaire|"
    r"trainee|apprentice\w*|phd|postdoc\w*|fellow\w*|thesis|masterarbeit|student|"
    r"co-?op|placement|estágio|estagiário\w*|becari[oa])\b"
    # A French internship title opens with "Stage": "Stage - IA Machine Learning…".
    # Anywhere else "stage" is English ("early-stage start-up").
    r"|^\s*stage\b",
    re.I,
)

#: Business roles that borrow technical words. Beaten by a tech noun in the
#: same title ("Engineering Manager" is still engineering — and senior).
_BUSINESS_ROLE = re.compile(
    r"\b(?:sales|account (?:executive|manager)|marketing|recruiter|recruiting|talent|"
    r"strategist|consultant|designer|banking|finance|legal|counsel|lawyer|"
    r"product manager|product owner|customer success|business development|"
    r"partnerships?|community|evangelist|copywriter|communications?|affiliate|"
    r"coordinator|assistant|administrator|support agent|operations manager|"
    r"project manager|program manager|scrum master|seo|content|writer|trainer|"
    r"teacher|tutor|annotator|labell?er|rater|evaluator|engagement)\b",
    re.I,
)

_CUSTOMER_FACING = re.compile(
    r"\b(?:value\s+engineer|forward[- ]deployed|solutions?\s+(?:architect|engineer|consultant)|"
    r"sales\s+engineer|pre-?sales|customer\s+engineer|ai\s+architect|technical\s+account|"
    r"implementation\s+(?:engineer|consultant)|field\s+engineer)\b", re.I)

_OPT_BUSINESS = re.compile(
    r"\b(?:marketing|campaigns?|conversion|seo|search engine|adwords|ppc|resource|process|tax|"
    r"fiscal|revenue|cash|claims|digital|cro|crm|budget|workforce|inventory)\b", re.I)
_OPT_TECHNICAL = re.compile(r"\b(?:engineer|scientist|research|developer|mathematician|"
                            r"modell?er|algorithm)\w*", re.I)

#: Titles that say nothing about the discipline, for which the body decides.
_GENERIC_TITLE = re.compile(
    r"^\s*(?:(?:senior|junior|graduate|lead|staff|principal|associate)\s+)?"
    r"(?:engineer|scientist|researcher|research engineer|research scientist|"
    r"member of (?:the )?technical staff|mts|developer|software engineer|"
    r"technical staff|founding engineer)\b",
    re.I,
)

#: Body evidence for a generic title: (label, pattern). Counted, not just found.
_BODY_EVIDENCE: dict[str, re.Pattern[str]] = {
    "MACHINE_LEARNING": re.compile(r"machine learning|deep learning|pytorch|tensorflow|"
                                   r"neural network|model training|\b(?-i:ML)\b", re.I),
    "AI": re.compile(r"\b(?-i:AI)\b|\bllms?\b|large language model|generative|"
                     r"foundation model", re.I),
    "DATA_SCIENCE": re.compile(r"data scien|statistic|predictive model|experimentation", re.I),
    "OPTIMIZATION": re.compile(r"optimi[sz]ation|operations research|solver|linear programming",
                               re.I),
    "SIMULATION": re.compile(r"simulation|monte carlo|digital twin", re.I),
}

#: Labels implied by others, per §13's own example.
_IMPLIES = {
    "MACHINE_LEARNING": ("AI",),
}

_COMPILED = {label: [re.compile(p, re.I) for p in pats] for label, pats in _TITLE_DOMAIN.items()}


@dataclass(frozen=True)
class RoleClass:
    labels: tuple[str, ...] = ("OTHER",)
    confidence: float = 0.0
    evidence: str = ""
    #: True when the title alone could not say, and the body was read.
    from_description: bool = False

    @property
    def primary(self) -> str:
        return self.labels[0] if self.labels else "OTHER"


def _title_labels(title: str) -> list[str]:
    found: list[str] = []
    for label, patterns in _COMPILED.items():
        if any(p.search(title) for p in patterns):
            found.append(label)
    return found


def classify(title: str | None, description: str | None = None) -> RoleClass:
    """Label one posting. Pure — the §89 cases are tested against it."""
    title = " ".join((title or "").split())
    # Gender markers and bracketed noise carry no signal and confuse nothing,
    # but "(m/w/d)" contains a "w" and "d" — strip before matching.
    clean = re.sub(r"\((?:[mwfdhxn]\s*[/|,]\s*){1,3}[mwfdhxn]\)|\(all genders\)|"
                   r"\b[mwf]/[mwf]/[dx]\b", " ", title, flags=re.I)
    labels = _title_labels(clean)
    tech = bool(_TECH_ROLE.search(clean))
    trainee = bool(_TRAINEE_ROLE.search(clean))
    business = bool(_BUSINESS_ROLE.search(clean))

    # Domain words that carry their own role noun count without a separate one.
    self_naming = {"DATA_SCIENCE", "APPLIED_SCIENCE", "DATA_ENGINEERING",
                   "SOFTWARE_ENGINEERING", "ANALYTICS", "BI", "DECISION_SCIENCE"}
    if labels and not (tech or trainee):
        labels = [lbl for lbl in labels if lbl in self_naming]
    if labels and business and not tech:
        labels = []
    # Customer-facing roles named like engineering ones: "Associate Applied (AI)
    # Value Engineer", "Forward Deployed Machine Learning Engineer", "Applied AI
    # Architect, Enterprise". Pre-sales and account work, whatever the noun
    # (Simon's rating, and three more in the 28 Sep 2026 sample).
    if labels and _CUSTOMER_FACING.search(clean):
        return RoleClass(("OTHER",), 0.5, f"customer-facing: {title}")
    # "Marketing Optimisation Specialist", "Resource Optimization Specialist":
    # optimising campaigns, rosters or spend, not mathematical optimisation.
    # Only when no engineering or science noun says otherwise.
    if set(labels) <= {"OPTIMIZATION"} and labels and _OPT_BUSINESS.search(clean) \
            and not _OPT_TECHNICAL.search(clean):
        return RoleClass(("OTHER",), 0.4, f"business optimisation: {title}")

    # "Research Scientist, Quantum Chemistry" is applied science — just not
    # ours. With no domain word in the title, the body has to mention one.
    if labels == ["APPLIED_SCIENCE"] and description:
        hits = sum(len(p.findall(description)) for p in _BODY_EVIDENCE.values())
        if hits == 0:
            return RoleClass(("OTHER",), 0.5,
                             "a research role with no ML, data or optimisation in the posting")

    if labels:
        # Implied labels, and the engineering half of an "… Engineer" title.
        for label in list(labels):
            for implied in _IMPLIES.get(label, ()):
                if implied not in labels:
                    labels.append(implied)
        if re.search(r"\bengineer", clean, re.I) and "SOFTWARE_ENGINEERING" not in labels \
                and any(lbl in labels for lbl in ("MACHINE_LEARNING", "AI")):
            labels.append("SOFTWARE_ENGINEERING")
        return RoleClass(tuple(_ordered(labels)), 0.9, f"title: {title}")

    if _GENERIC_TITLE.search(clean) and description and not business:
        counts = {label: len(p.findall(description)) for label, p in _BODY_EVIDENCE.items()}
        strong = [label for label, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 3]
        if strong:
            base = "SOFTWARE_ENGINEERING" if re.search(r"engineer|developer", clean, re.I) \
                else "APPLIED_SCIENCE"
            labels = [*strong, base]
            for label in list(labels):
                for implied in _IMPLIES.get(label, ()):
                    if implied not in labels:
                        labels.append(implied)
            evidence = ", ".join(f"{label.lower()} ×{counts[label]}" for label in strong)
            return RoleClass(tuple(_ordered(labels)), 0.55,
                             f"generic title; the description mentions {evidence}", True)
        if re.search(r"engineer|developer", clean, re.I):
            return RoleClass(("SOFTWARE_ENGINEERING",), 0.4, f"title: {title}", True)
    return RoleClass(("OTHER",), 0.3 if title else 0.0,
                     "business role" if business else f"title: {title}")


def _ordered(labels: list[str]) -> list[str]:
    seen = dict.fromkeys(labels)
    return [label for label in LABELS if label in seen]
