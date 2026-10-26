def flowdefs: {
  "openapi": {"nodeKeys": ["specs"], "agent": "openapi-spec-workflow", "project_dir": "agents/hosted/openapi/openapi-spec-workflow"},
  "dbschema": {"nodeKeys": ["dbschema", "db-schema"], "agent": "dbschema-workflow", "project_dir": "agents/hosted/dbschema/dbschema-workflow"},
  "eventcatalog": {"nodeKeys": ["eventcatalog", "event-catalog"], "agent": "eventcatalog-workflow", "project_dir": "agents/hosted/eventcatalog/eventcatalog-workflow"},
  "service-dependency": {"nodeKeys": ["service-dependencies"], "agent": "service-dependency-workflow", "project_dir": "agents/hosted/servicedependencies/service-dependency-workflow"},
  "local-dev-config": {"nodeKeys": ["local-dev-config"], "agent": "local-dev-config-workflow", "project_dir": "agents/hosted/localdevconfig/local-dev-config-workflow"},
  "user-journeys": {"nodeKeys": ["user-journeys"], "agent": "user-journeys-workflow", "project_dir": "agents/hosted/userjourneys/user-journeys-workflow"},
  "repo-metadata": {"nodeKeys": ["repo-metadata"], "agent": "repo-metadata-workflow", "project_dir": "agents/hosted/repometadata/repo-metadata-workflow"}
};

# Inputs: $repo (github-repo to match) and $keys (requested flow keys, or null for all).
# Reads the manifest array from stdin/file and resolves it to a matrix of
# {key, agent, project_dir, source_url} entries for the matched repository,
# using each flow's own "path-to-scan" from the manifest -- not one shared source URL.
# project_dir is required: `azd ai agent invoke <name>` resolves <name> against the
# azure.yaml in the current working directory, not just the Foundry project endpoint.
(($repo | rtrimstr("/"))) as $wantRepo
| (map(select((.["github-repo"] // "" | rtrimstr("/")) == $wantRepo)) | first) as $entry
| if $entry == null then
    {error: ("No manifest entry found for github-repo " + $repo)}
  else
    (flowdefs
     | to_entries
     | map(
         . as $f
         | ($entry | to_entries | map(select(.key as $k | $f.value.nodeKeys | index($k))) | first) as $match
         | select($match != null)
         | select($keys == null or ($keys | index($f.key)) != null)
         | {
             key: $f.key,
             agent: $f.value.agent,
             project_dir: $f.value.project_dir,
             source_url: (($entry["github-repo"] | rtrimstr("/")) + "/" + $match.value["path-to-scan"])
           }
       )) as $matrix
    | if ($matrix | length) == 0 then
        {error: "Manifest entry matched but no requested flow nodes were present"}
      else
        {matrix: $matrix}
      end
  end
