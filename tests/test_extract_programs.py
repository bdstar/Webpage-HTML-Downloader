import json

from extract_programs import extract_programs, main

CARD = """
<section class="Card ProgrammeCard" data-v-237319ac="">
  <!--[-->
  <div class="OrganisationInfo">
    <strong class="OrganisationName SkeletonBlock block7" title="{uni}"> {uni_text} </strong>
    <span class="RatingCount"><b class="SkeletonBlock block8">4,2</b></span>
  </div>
  <span class="Locations SkeletonBlock block10"> Brighton,
     United Kingdom <!-- --> </span>
  <h2 class="Title SkeletonBlock block12"> {title} </h2>
  <div class="ProgrammeFacts">
    <span aria-label="Full-time" class="Tag default"> Full-time <span class="Counter">+2</span></span>
    <span aria-label="{degree}" class="Tag default"><!-- --> {degree} <!-- --></span>
    <span aria-label="On-campus" class="Tag default"> On-campus </span>
  </div>
  <div class="ProgrammePrice">
    <span class="Duration">1 year</span>
    <span class="CurrentPrice"><b>{price}</b> /yr</span>
  </div>
  <a class="VisitProgramme" href="{href}" target="_blank">View Programme Information</a>
</section>
"""


def card(uni="University of Brighton", uni_text=None, title="Management", degree="M.Sc.",
         price="BDT&nbsp;3,067,027", href="https://www.mastersportal.com/studies/11264/management.html"):
    return CARD.format(uni=uni, uni_text=uni_text if uni_text is not None else uni,
                       title=title, degree=degree, price=price, href=href)


def page(*cards):
    return "<html><body><main>" + "".join(cards) + "</main></body></html>"


def test_extracts_all_fields_and_ignores_rating_and_pace_tags():
    result = extract_programs(page(card()))
    assert result.records == [{
        "university_name": "University of Brighton",
        "university_location": "Brighton, United Kingdom",
        "program_name": "Management",
        "program_type": "M.Sc.",
        "cost": "BDT 3,067,027",
        "program_url": "https://www.mastersportal.com/studies/11264/management.html",
    }]


def test_unavailable_price_is_null_but_record_kept():
    result = extract_programs(page(card(price="Tuition not available")))
    assert len(result.records) == 1
    assert result.records[0]["cost"] is None
    assert result.missing_counts["cost"] == 1
    assert result.missing_required == []


def test_relative_url_resolved_and_title_fallback_to_text():
    html = page(card(href="/studies/1/x.html").replace('title="University of Brighton"', ""))
    record = extract_programs(html).records[0]
    assert record["program_url"] == "https://www.mastersportal.com/studies/1/x.html"
    assert record["university_name"] == "University of Brighton"


def test_missing_required_reported_and_empty_cards_skipped():
    missing_uni = card().replace('<strong class="OrganisationName SkeletonBlock block7" '
                                 'title="University of Brighton"> University of Brighton </strong>', "")
    empty = '<section class="Card ProgrammeCard"><div class="SkeletonBlock"></div></section>'
    result = extract_programs(page(missing_uni, empty))
    assert result.cards_found == 2
    assert len(result.records) == 1
    assert result.records[0]["university_name"] is None
    assert result.missing_required == [(1, ["university_name"])]
    assert result.skipped_cards == [2]


def test_duplicate_urls_reported_not_merged():
    result = extract_programs(page(card(title="A"), card(title="B"), card(title="A", href="/studies/2/a.html")))
    assert len(result.records) == 3
    assert list(result.duplicate_urls.values()) == [[1, 2]]


def test_cli_writes_utf8_json_next_to_input(tmp_path, capsys):
    source = tmp_path / "page.html"
    source.write_text(page(card(uni="Université de Test")), encoding="utf-8")
    assert main([str(source)]) == 0
    data = json.loads((tmp_path / "programs.json").read_text(encoding="utf-8"))
    assert data[0]["university_name"] == "Université de Test"
    out = capsys.readouterr().out
    assert "Programme cards found: 1" in out and "Status: Completed" in out
    assert main([str(source)]) == 0  # rerun overwrites cleanly
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_custom_output_and_errors(tmp_path):
    source = tmp_path / "page.html"
    source.write_text(page(card()), encoding="utf-8")
    out = tmp_path / "nested" / "dir" / "out.json"
    assert main([str(source), "--output", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 1
    assert main([str(tmp_path / "missing.html")]) == 1
    (tmp_path / "other.html").write_text("<html><p>no cards</p></html>", encoding="utf-8")
    assert main([str(tmp_path / "other.html")]) == 2
