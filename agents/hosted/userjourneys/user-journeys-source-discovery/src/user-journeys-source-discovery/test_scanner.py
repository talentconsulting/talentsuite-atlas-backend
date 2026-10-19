import io
import unittest
import zipfile

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


if __name__ == "__main__":
    unittest.main()
