# Inputs: $repo (github-repo of the entry), $node (its manifest node, e.g. "specs"), $commit (the
# 40-character SHA that was scanned) and $path (the manifest's path in the data repository).
# Reads the manifest array and returns the manifestFile/manifestNodes fields a *-workflow agent
# forwards to its PR creator, so the scanned commit is recorded in the same pull request as the
# generated files. The PR creator applies only $node, to the manifest as it is when the PR opens.
(($repo | rtrimstr("/"))) as $wantRepo
| {
    manifestFile: {
      path: $path,
      content: map(
        if ((.["github-repo"] // "") | rtrimstr("/")) == $wantRepo and (.[$node] | type) == "object"
        then .[$node] |= (.["last-commit-hash-scanned"] = $commit | del(.["in-progress"]))
        else .
        end
      )
    },
    manifestNodes: [$node]
  }
