"""Invariants the docs state, checked on generated inputs. Hypothesis is a dev dependency (`uv sync`): without it,
plain `python3 -m unittest` and the Python 3.9 runtime job skip the property classes. Under CI, Hypothesis loads its
derandomized `ci` profile; HYPOTHESIS_PROFILE=extended runs 2000 examples per property, =mutation is make mutation's."""

import os
import string
import typing
import unittest

from support import load_hook

from qa_sdet import contracts, core, ingest, report, scrub


class ContractsMatchCode(unittest.TestCase):
    """contracts.py describes the JSON shapes for the type checker; its Literals must stay the runtime's values."""

    def test_each_literal_is_its_runtime_tuple(self):
        self.assertEqual(typing.get_args(contracts.CaseStatus), core.CASE_STATUSES)
        self.assertEqual(set(typing.get_args(contracts.BugStatus)), core.STATUSES)
        self.assertEqual(typing.get_args(contracts.StopReason), ingest.STOP_REASONS)
        self.assertEqual(typing.get_args(contracts.BlockScope), ingest.BLOCK_SCOPES)
        self.assertEqual(set(typing.get_args(contracts.Verdict)), set(report.VERDICT_ICON))


try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
except ImportError:
    given = None

if given is None:

    class Properties(unittest.TestCase):
        @unittest.skip("Hypothesis is a dev dependency (uv sync); this run has the standard library only")
        def test_properties(self):
            pass

else:
    settings.register_profile("extended", max_examples=2000, deadline=None)
    # make mutation: the same examples for every mutant; mutmut's harness reruns a test method on new instances
    settings.register_profile(
        "mutation",
        max_examples=50,
        derandomize=True,
        deadline=None,
        suppress_health_check=[HealthCheck.differing_executors],
    )
    if os.environ.get("HYPOTHESIS_PROFILE"):
        settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])

    TOKEN = st.text(string.ascii_letters + string.digits, min_size=20, max_size=40).filter(
        lambda t: any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
    )
    AROUND = st.text(string.ascii_letters + string.digits + " .,;:-_/()[]", max_size=20)
    SECRET_FORMS = (  # what scrub.redact promises to remove (its docstrings and LITERAL_REDACTIONS)
        "Authorization: Bearer {}",
        "curl -H 'X-Api-Key: {}' https://qa.example.com",
        "Cookie: sid={}",
        "Bearer {}",
        "https://bob:{}@qa.example.com/x",
        "GET /x?access_token={}&page=2",
        "export API_KEY={}",
    )

    class Redaction(unittest.TestCase):
        @given(st.text())
        def test_redacting_twice_changes_nothing(
            self, text
        ):  # scrub_tree rescrubs files: a second pass must be a no-op
            once = scrub.redact(text)
            self.assertEqual(scrub.redact(once), once)

        @given(TOKEN, st.sampled_from(SECRET_FORMS), AROUND, AROUND)
        def test_a_planted_credential_never_survives(self, token, form, before, after):
            assume(token not in before + after)  # the token only where the form puts it
            text = f"{before} {form.format(token)} {after}"
            self.assertNotIn(token, scrub.redact(text))

        @given(TOKEN, TOKEN, TOKEN)
        def test_a_jwt_never_survives(self, header, body, signature):
            jwt = f"eyJ{header}.{body}.{signature}"
            self.assertNotIn(jwt, scrub.redact(f"token {jwt} end"))

    HOSTS = core.allowed_hosts({"allowedHosts": ["qa.example.com", "*.api.example.com", "localhost"]})
    URLS = st.builds(
        "".join,
        st.tuples(
            st.sampled_from(["http://", "https://", "HTTPS://", "ws://", "javascript://", "ftp://", "//", ""]),
            st.sampled_from(["", "user@", "u:p@", "qa.example.com@"]),
            st.sampled_from(
                [
                    "qa.example.com",
                    "sub.qa.example.com",
                    "QA.EXAMPLE.COM.",
                    "evilqa.example.com",
                    "v1.api.example.com",
                    "evil.com",
                    "localhost",
                    "127.0.0.1",
                    "[::1]",
                    "qa.example.com%2f.evil.com",
                ]
            ),
            st.sampled_from(["", ":8443", ":0"]),
            st.text("abc/?#@\\%.:=&", max_size=12),
        ),
    )

    class HostRulesMatchTheGuard(unittest.TestCase):
        """LibMirrorsGuard's hand-picked URLs, generated: lib's url_allowed (session-check, dispatch, api-probe) must
        decide as the guard's host_ok does, for http(s)."""

        guard = load_hook("guard_properties")

        @given(URLS)
        def test_lib_and_guard_agree(self, url):
            want = self.guard.host_ok(url, HOSTS) and url.lower().startswith(("http://", "https://"))
            self.assertEqual(core.url_allowed(url, HOSTS), want)

    class Normalisation(unittest.TestCase):
        """Routes and builds are DB keys: normalising a stored value again must give the same key."""

        @given(st.text())
        def test_norm_route_is_idempotent(self, route):
            once = core.norm_route(route)
            self.assertEqual(core.norm_route(once), once)

        @given(st.text())
        def test_norm_build_is_idempotent(self, build):
            once = core.norm_build(build)
            self.assertEqual(core.norm_build(once), once)

    class FramedLocators(unittest.TestCase):
        """A crawl saves a locator behind its frame; the card finds the code map's row for it by taking the frame off."""

        @given(st.text().filter(lambda css: core.FRAME_MARK not in css), st.integers(0, 4))
        def test_any_css_comes_back_out_of_its_frame(self, css, n):
            self.assertEqual(core.frame_tail(core.in_frame(css, n, True)), css)

    RANK = {"FAIL": 0, "BLOCKED": 1, "PASS WITH NOTES": 2, "PASS": 3}  # worst first
    WORSE = {"PASS": ("QUESTION", "NOT_RUN", "BLOCKED", "FAIL"), "QUESTION": ("NOT_RUN", "BLOCKED", "FAIL")}

    def one_ticket(statuses, n_acs):
        """One ticket with n_acs ACs and a case per status, every AC covered: no bugs, no questions, no runs."""
        ticket = {"key": "PROJ-1", "acs": [{"id": f"AC-{i + 1}", "text": "x"} for i in range(n_acs)]}
        cases = [
            {"id": f"TC-{i + 1}", "ticket": "PROJ-1", "ac": f"AC-{i % n_acs + 1}", "route": "/form", "status": s}
            for i, s in enumerate(statuses)
        ]
        return ticket, cases

    STATUSES = st.lists(st.sampled_from(core.CASE_STATUSES), min_size=1, max_size=8)

    class VerdictRules(unittest.TestCase):
        """README "Reading REPORT.md", verdict per ticket, first match wins."""

        @given(STATUSES, st.integers(1, 4))
        def test_the_first_matching_rule_decides(self, statuses, n_acs):
            n_acs = min(n_acs, len(statuses))
            ticket, cases = one_ticket(statuses, n_acs)
            v, _ = report.verdict(ticket, cases, [])
            if "FAIL" in statuses:
                self.assertEqual(v, "FAIL")
            elif {"BLOCKED", "NOT_RUN"} & set(statuses):
                self.assertEqual(v, "BLOCKED")
            elif "QUESTION" in statuses:
                self.assertEqual(v, "PASS WITH NOTES")
            else:
                self.assertEqual(v, "PASS")

        @given(STATUSES, st.integers(1, 4), st.data())
        def test_a_worse_case_result_never_improves_the_verdict(self, statuses, n_acs, data):
            n_acs = min(n_acs, len(statuses))
            better = [i for i, s in enumerate(statuses) if s in WORSE]
            if not better:
                return
            i = data.draw(st.sampled_from(better))
            worse = [*statuses[:i], data.draw(st.sampled_from(WORSE[statuses[i]])), *statuses[i + 1 :]]
            before, _ = report.verdict(*one_ticket(statuses, n_acs), [])
            after, _ = report.verdict(*one_ticket(worse, n_acs), [])
            self.assertLessEqual(RANK[after], RANK[before], (statuses, worse))
