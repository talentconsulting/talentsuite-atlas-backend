import io
import unittest
import zipfile
from unittest import mock

import scanner
from navigation import extract, find_web_projects
from scanner import ScanError, scan


SOURCE = "https://github.com/source/app/tree/main/src"
WEB = "src/App.Web"

ROUTE_NAMES = """namespace App.Web.Infrastructure;
public static class RouteNames
{
    public const string Home = "home";
    public const string Apply = "apply";
    public const string StubSignIn = "stub-sign-in";
    public static class ApplyApprenticeship
    {
        public const string Jobs = nameof(Jobs);
        public const string Preview = "preview";
    }
}
"""

APPLY_CONTROLLER = """using Microsoft.AspNetCore.Authorization;
namespace App.Web.Controllers;

[Authorize]
[Route("applications/{id}", Name = RouteNames.Apply)]
public class ApplyController : Controller
{
    private const string PreviewView = "~/Views/Apply/Preview.cshtml";

    public IActionResult Index() { return View(); }

    [HttpGet("preview", Name = RouteNames.ApplyApprenticeship.Preview)]
    public IActionResult Preview() { return View(PreviewView); }

    [HttpPost("preview", Name = RouteNames.ApplyApprenticeship.Preview)]
    public IActionResult Submit(bool ok)
    {
        return RedirectToRoute(ok ? RouteNames.Home : RouteNames.Apply);
    }
}
"""

JOBS_CONTROLLER = """using static App.Web.Infrastructure.RouteNames;
[Authorize]
public class JobsController : Controller
{
    [HttpGet("jobs", Name = ApplyApprenticeship.Jobs)]
    public IActionResult Jobs() => View();

    [HttpPost("jobs", Name = ApplyApprenticeship.Jobs)]
    public IActionResult Save()
    {
        if (!ModelState.IsValid) return RedirectToRoute(ApplyApprenticeship.Jobs);
        return RedirectToRoute
            (RouteNames.Apply);
    }
}
"""

HOME_CONTROLLER = """[AllowAnonymous]
public class HomeController : Controller
{
    [HttpGet("", Name = RouteNames.Home)]
    public IActionResult Index() { return View(); }

    [HttpGet("stub", Name = RouteNames.StubSignIn)]
    public IActionResult Stub() { return View(); }
}
"""

CERTIFICATES_CONTROLLER = """public class CertificatesController : Controller
{
    public const string ListRouteGet = nameof(ListRouteGet);
    public const string ShareRouteGet = nameof(ShareRouteGet);
    public const string ShareRoutePost = nameof(ShareRoutePost);

    [HttpGet("certificates", Name = ListRouteGet)]
    public IActionResult List() { return View(); }

    [HttpGet("share", Name = ShareRouteGet)]
    public IActionResult Share() { return View(); }

    [HttpPost("share", Name = ShareRoutePost)]
    public IActionResult ShareSubmit() { return RedirectToRoute(ListRouteGet); }
}
"""

FILES = {
    f"{WEB}/App.Web.csproj": "<Project />",
    f"{WEB}/Infrastructure/RouteNames.cs": ROUTE_NAMES,
    f"{WEB}/Controllers/ApplyController.cs": APPLY_CONTROLLER,
    f"{WEB}/Controllers/JobsController.cs": JOBS_CONTROLLER,
    f"{WEB}/Controllers/HomeController.cs": HOME_CONTROLLER,
    f"{WEB}/Controllers/CertificatesController.cs": CERTIFICATES_CONTROLLER,
    f"{WEB}/Views/Apply/Index.cshtml": '<a asp-route="@RouteNames.ApplyApprenticeship.Jobs">Jobs</a>\n<partial model="x" name="_Task" />\n',
    f"{WEB}/Views/Shared/_Task.cshtml": '<partial name="_Inner" />\n',
    f"{WEB}/Views/Shared/_Inner.cshtml": '<a href="@Url.RouteUrl(RouteNames.ApplyApprenticeship.Preview)">Preview</a>\n',
    f"{WEB}/Views/Apply/Preview.cshtml": '<form asp-route="@RouteNames.ApplyApprenticeship.Preview" method="post"></form>\n',
    f"{WEB}/Views/Jobs/Jobs.cshtml": '<a asp-route="@RouteNames.Apply">Back</a>\n',
    f"{WEB}/Views/Home/Index.cshtml": '<a asp-route="@RouteNames.Apply">Start</a>\n',
    f"{WEB}/Views/Certificates/List.cshtml": '<a asp-action="Share">Share</a>\n',
    f"{WEB}/Views/Certificates/Share.cshtml": "<p>share</p>\n",
    f"{WEB}/Views/Shared/_Layout.cshtml": '<a asp-route="@RouteNames.Home">Home</a>\n',
    # An API project has controllers but no pages, and a test project is ignored.
    "src/App.Api/App.Api.csproj": "<Project />",
    "src/App.Api/Controllers/ThingsController.cs": "public class ThingsController : ControllerBase {}",
    "src/App.Web.UnitTests/App.Web.UnitTests.csproj": "<Project />",
    "src/App.Web.UnitTests/Views/Fake.cshtml": "",
}

RAZOR = "src/Portal.Web"
RAZOR_FILES = {
    f"{RAZOR}/Portal.Web.csproj": "<Project />",
    f"{RAZOR}/Startup.cs": 'options.Conventions.AuthorizeFolder("/").AllowAnonymousToPage("/Index");',
    f"{RAZOR}/Pages/Index.cshtml": "@page\n<h1>Welcome</h1>",
    f"{RAZOR}/Pages/Index.cshtml.cs": 'public class IndexModel : PageModel { public IActionResult OnGet() => RedirectToPage("/Apprenticeships/Index"); }',
    f"{RAZOR}/Pages/Apprenticeships/Index.cshtml": '@page "/apprenticeships"\n',
    f"{RAZOR}/Pages/Apprenticeships/Index.cshtml.cs": 'public IActionResult OnGet() { if (x) return RedirectToPage("AccountNotFound"); return RedirectToPage("Terms"); }',
    f"{RAZOR}/Pages/Apprenticeships/Terms.cshtml": '@page\n<a asp-page="/Index">Home</a>',
    f"{RAZOR}/Pages/AccountNotFound.cshtml": "@page\n",
}


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for path, content in files.items():
            output.writestr(f"app-main/{path}", content)
    return buffer.getvalue()


def edges(graph):
    return {(t["from"], t["to"], t["trigger"]) for t in graph["transitions"]}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.graph = extract(FILES, WEB)
        self.pages = {p["id"]: p for p in self.graph["pages"]}

    def test_pages_are_named_routes_with_get_and_post_merged(self):
        self.assertEqual({"Apply", "ApplyApprenticeship.Preview", "ApplyApprenticeship.Jobs", "Home",
                          "CertificatesController.List", "CertificatesController.Share"}, set(self.pages))
        self.assertEqual(["GET", "POST"], self.pages["ApplyApprenticeship.Preview"]["methods"])

    def test_controller_class_route_name_applies_to_actions_without_a_template(self):
        self.assertEqual(["applications/{id}"], self.pages["Apply"]["routes"])

    def test_stub_pages_are_left_out(self):
        self.assertNotIn("StubSignIn", self.pages)

    def test_redirects_including_aliases_line_breaks_and_both_sides_of_a_ternary(self):
        found = edges(self.graph)
        self.assertIn(("ApplyApprenticeship.Jobs", "Apply", "redirect"), found)     # "RedirectToRoute\n(" form
        self.assertIn(("ApplyApprenticeship.Preview", "Home", "redirect"), found)
        self.assertIn(("ApplyApprenticeship.Preview", "Apply", "redirect"), found)
        self.assertIn(("CertificatesController.Share", "CertificatesController.List", "redirect"), found)

    def test_view_links_follow_nested_partials_and_same_controller_actions(self):
        found = edges(self.graph)
        self.assertIn(("Apply", "ApplyApprenticeship.Jobs", "link"), found)
        self.assertIn(("Apply", "ApplyApprenticeship.Preview", "link"), found)       # two partials deep
        self.assertIn(("CertificatesController.List", "CertificatesController.Share", "link"), found)

    def test_self_links_are_counted_not_drawn(self):
        self.assertNotIn(("ApplyApprenticeship.Jobs", "ApplyApprenticeship.Jobs", "redirect"), edges(self.graph))
        self.assertGreaterEqual(self.graph["selfLinkCount"], 2)

    def test_layout_links_are_site_navigation(self):
        self.assertEqual(["Home"], [link["to"] for link in self.graph["siteNavigation"]])

    def test_every_transition_cites_file_and_line(self):
        for t in self.graph["transitions"]:
            path, line = t["evidence"][0].rsplit(":", 1)
            self.assertIn(path, FILES)
            self.assertGreater(int(line), 0)

    def test_authentication_from_attributes_and_unknown_without_a_rule(self):
        self.assertTrue(self.pages["Apply"]["requiresAuthentication"])
        self.assertFalse(self.pages["Home"]["requiresAuthentication"])
        self.assertIsNone(self.pages["CertificatesController.List"]["requiresAuthentication"])

    def test_entry_points_are_exactly_the_pages_nothing_leads_to(self):
        incoming = {t["to"] for t in self.graph["transitions"]}
        for page in self.graph["pages"]:
            self.assertEqual(page["id"] not in incoming, page["isEntryPoint"], page["id"])
        self.assertFalse(self.pages["Apply"]["isEntryPoint"])

    def test_razor_pages_resolve_relative_names_and_folder_wide_authorization(self):
        graph = extract(RAZOR_FILES, RAZOR)
        pages = {p["id"]: p for p in graph["pages"]}
        found = edges(graph)
        self.assertIn(("/Index", "/Apprenticeships/Index", "redirect"), found)
        self.assertIn(("/Apprenticeships/Index", "/AccountNotFound", "redirect"), found)   # found one folder up
        self.assertIn(("/Apprenticeships/Index", "/Apprenticeships/Terms", "redirect"), found)
        self.assertIn(("/Apprenticeships/Terms", "/Index", "link"), found)
        self.assertEqual(["/apprenticeships"], pages["/Apprenticeships/Index"]["routes"])
        self.assertFalse(pages["/Index"]["requiresAuthentication"])
        self.assertTrue(pages["/Apprenticeships/Terms"]["requiresAuthentication"])

    def test_route_constants_from_a_shared_project_literals_nameof_and_get_post_suffixes(self):
        files = {
            "src/Shared/Helpers/RouteConstants.cs": 'public static class RouteConstants { public const string Home = "Home"; public const string Results = "results-page"; }',
            "src/Site.Web/Site.Web.csproj": "",
            "src/Site.Web/Controllers/HomeController.cs": """public class HomeController : Controller
{
    [HttpGet("", Name = RouteConstants.Home)]
    public IActionResult Index() { return View(); }

    [HttpGet("results", Name = "results-page")]
    public IActionResult Results() { if (x) return RedirectToRoute(RouteConstants.PageNotFound); return RedirectToRoute(nameof(RouteConstants.Home)); }
}
""",
            "src/Site.Web/Controllers/FeedbackController.cs": """public class FeedbackController : Controller
{
    public const string QuestionGet = "question-get";
    public const string QuestionPost = "question-post";
    [HttpGet("q", Name = QuestionGet)]
    public IActionResult Question() => View();
    [HttpPost("q", Name = QuestionPost)]
    public IActionResult Answer() { return RedirectToRoute(RouteConstants.Results); }
}
""",
            "src/Site.Web/Views/Home/Index.cshtml": '<a asp-route="results-page">Results</a> <a asp-route="Home">Home</a>',
            "src/Site.Web/Views/Feedback/Question.cshtml": "",
        }
        graph = extract(files, "src/Site.Web")
        found = edges(graph)
        self.assertEqual({"Home", "Results", "FeedbackController.Question"}, {p["id"] for p in graph["pages"]})
        self.assertIn(("Home", "Results", "link"), found)                          # literal value of a constant
        self.assertIn(("Results", "Home", "redirect"), found)                      # nameof(RouteConstants.Home)
        self.assertIn(("FeedbackController.Question", "Results", "redirect"), found)
        self.assertEqual([], graph["unresolved"])                                  # PageNotFound is ignored, not unresolved

    def test_get_and_post_suffixes_with_separators_name_one_page(self):
        from navigation import _Extractor
        self.assertEqual("Landing", _Extractor.page_key("Landing_Get"))
        self.assertEqual("QuestionOne", _Extractor.page_key("QuestionOne_Post"))
        self.assertEqual("Landing_Get_New", _Extractor.page_key("Landing_Get_New"))
        self.assertEqual("Share", _Extractor.page_key("ShareRoutePost"))

    def test_web_projects_need_pages_or_views(self):
        self.assertEqual([WEB], find_web_projects(list(FILES)))


class ScanTests(unittest.TestCase):
    def test_scan_returns_graph_and_batches_covering_every_page(self):
        result = scan(SOURCE, archive(FILES))
        self.assertEqual([WEB], result["webProjects"])
        page_ids = {p["id"] for p in result["graph"]["pages"]}
        batched = [pid for batch in result["batches"] for pid in batch["pageIds"]]
        self.assertEqual(sorted(page_ids), sorted(batched))
        for batch in result["batches"]:
            self.assertTrue(all(url.startswith("https://github.com/source/app/blob/main/src/") for url in batch["files"]))
        self.assertNotIn("pageFiles", result["graph"])

    def test_several_web_projects_get_prefixed_page_ids(self):
        result = scan(SOURCE, archive({**FILES, **RAZOR_FILES}))
        ids = {p["id"] for p in result["graph"]["pages"]}
        self.assertIn("App.Web:Apply", ids)
        self.assertIn("Portal.Web:/Index", ids)

    def test_a_repository_without_web_projects_has_an_empty_graph(self):
        result = scan(SOURCE, archive({"src/App.Api/App.Api.csproj": "", "src/App.Api/Controllers/A.cs": ""}))
        self.assertEqual([], result["webProjects"])
        self.assertEqual([], result["graph"]["pages"])

    def test_rejects_non_tree_urls(self):
        with self.assertRaises(ScanError) as caught:
            scan("https://github.com/source/app/blob/main/src/x.cs", archive({}))
        self.assertEqual("invalid_source_url", caught.exception.code)


FORM_VIEW = """@model App.Web.Models.DetailsViewModel
<form asp-route="@RouteNames.Apply" method="post">
  <input type="hidden" asp-for="Id" />
  <label class="govuk-label" asp-for="FirstName">First name</label>
  <input class="govuk-input" asp-for="FirstName" />
  <label class="govuk-label" for="Email">Email address</label>
  <input class="govuk-input" asp-for="Email" type="email" id="Email" />
  <fieldset class="govuk-fieldset">
    <legend class="govuk-fieldset__legend">Do you have a <strong>disability</strong>?</legend>
    <input class="govuk-radios__input" type="radio" asp-for="HasDisability" value="true" id="HasDisability-yes" />
    <label class="govuk-label govuk-radios__label" for="HasDisability-yes">Yes</label>
    <input class="govuk-radios__input" type="radio" asp-for="HasDisability" value="false" id="HasDisability-no" />
    <label class="govuk-label govuk-radios__label" for="HasDisability-no">No</label>
  </fieldset>
  <select asp-for="Country"><option value="">Choose</option><option value="EN">England</option><option value="WA">Wales</option></select>
  @Html.TextAreaFor(m => m.Notes)
  <govuk-radios asp-for="Contact"><govuk-radios-fieldset><govuk-radios-fieldset-legend>How should we contact you?</govuk-radios-fieldset-legend>
    <govuk-radios-item value="phone">Phone</govuk-radios-item><govuk-radios-item value="post">Post</govuk-radios-item></govuk-radios-fieldset></govuk-radios>
  <button type="submit">Continue</button>
</form>
"""

FORM_MODELS = """namespace App.Web.Models;
public class BaseModel { [Required] public string Id { get; set; } }
public class DetailsViewModel : BaseModel
{
    [Required]
    [MaxLength(50)]
    [Display(Name = "Given name")]
    public string FirstName { get; set; }
    [EmailAddress] public string Email { get; set; }
    public bool? HasDisability { get; set; }
    public string Country { get; set; }
    public string Notes { get; set; }
    public string Contact { get; set; }
}
public class DetailsValidator : AbstractValidator<DetailsViewModel> { }
"""


class FormFieldTests(unittest.TestCase):
    def setUp(self):
        from forms import ModelIndex, describe_fields, view_fields, view_model_type
        models = ModelIndex({"src/App.Web/Models/Details.cs": FORM_MODELS})
        self.models = models
        self.fields = {f["name"]: f for f in describe_fields(view_fields(FORM_VIEW), models.properties(view_model_type(FORM_VIEW)))}

    def test_finds_visible_fields_and_leaves_out_hidden_ones(self):
        self.assertEqual(["FirstName", "Email", "HasDisability", "Country", "Notes", "Contact"], list(self.fields))

    def test_controls_and_labels(self):
        self.assertEqual(("text", "First name"), (self.fields["FirstName"]["control"], self.fields["FirstName"]["label"]))
        self.assertEqual(("email", "Email address"), (self.fields["Email"]["control"], self.fields["Email"]["label"]))
        self.assertEqual("Do you have a disability?", self.fields["HasDisability"]["label"])   # from the legend
        self.assertEqual("textarea", self.fields["Notes"]["control"])
        self.assertEqual(("radios", "How should we contact you?"), (self.fields["Contact"]["control"], self.fields["Contact"]["label"]))

    def test_fixed_options_with_their_labels(self):
        self.assertEqual([{"value": "true", "label": "Yes"}, {"value": "false", "label": "No"}], self.fields["HasDisability"]["options"])
        self.assertEqual(["EN", "WA"], [o["value"] for o in self.fields["Country"]["options"]])
        self.assertEqual([("phone", "Phone"), ("post", "Post")], [(o["value"], o["label"]) for o in self.fields["Contact"]["options"]])

    def test_view_model_attributes(self):
        first = self.fields["FirstName"]
        self.assertTrue(first["required"])
        self.assertEqual(["max 50 characters"], first["constraints"])
        self.assertEqual("string", first["dataType"])
        self.assertEqual(["email address"], self.fields["Email"]["constraints"])
        self.assertIsNone(self.fields["Country"]["required"])
        self.assertTrue(self.models.properties("DetailsViewModel")["Id"]["required"])   # inherited from the base class

    def test_validators_are_found_by_model(self):
        self.assertEqual("src/App.Web/Models/Details.cs", self.models.validators["DetailsViewModel"])

    def test_pages_get_fields_and_their_validator_in_the_describe_batch(self):
        files = {
            "src/Site.Web/Site.Web.csproj": "",
            "src/Site.Web/Models/Details.cs": FORM_MODELS,
            "src/Site.Web/Controllers/DetailsController.cs": """public class DetailsController : Controller
{
    [HttpGet("details", Name = "details")]
    public async Task<IActionResult> EditAsync() { return View(); }
}""",
            "src/Site.Web/Views/Details/Edit.cshtml": FORM_VIEW,   # found without the Async suffix
        }
        graph = extract(files, "src/Site.Web")
        page = graph["pages"][0]
        self.assertEqual(6, len(page["fields"]))
        self.assertEqual("src/Site.Web/Views/Details/Edit.cshtml", page["fields"][0]["sourceFile"])
        self.assertIn("src/Site.Web/Models/Details.cs", graph["pageFiles"]["details"])


class ArchiveDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"zip"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(scanner.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(scanner.urllib.request, "urlopen", side_effect=urlopen):
            scanner._download_archive(scanner.parse_source_url(SOURCE), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_downloads_through_the_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/source/app/zipball/main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))

    def test_without_a_token_the_public_archive_is_downloaded(self):
        request = self.download(None)
        self.assertEqual("https://codeload.github.com/source/app/zip/main", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
