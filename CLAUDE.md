# Agent instructions

## Issue workflow (every task, every agent)

Work is tracked as GitHub issues on the project board https://github.com/users/andreub/projects/1 (Status: Todo / In Progress / Done).

1. **Before starting**, look for an issue that covers the task: `gh issue list --state open --search "<keywords>"`.
   - **None exists:** create it with `gh issue create`, set its Type (below), and add it to the board.
   - **One exists and it is already In Progress:** another agent has it. Do not start; tell whoever gave you the task.
2. **Claim it:** set its board Status to **In Progress** before writing any code, so no other agent picks it up.
3. **When finished** (PR opened, or work merged): set Status to **Done**. Reference it in the PR body with `Closes #N`.

Commands (project 1, owner `andreub`):

```sh
# add an issue to the board (prints the item id; idempotent)
gh project item-add 1 --owner andreub --url <issue-url> --format json -q .id
# set Status
gh project item-edit --project-id PVT_kwHOAFwuvc4BtAFF --id <item-id> \
  --field-id PVTSSF_lAHOAFwuvc4BtAFFzhrLjcQ --single-select-option-id <option>
# options: Todo=f75ad846  In Progress=47fc9ee4  Done=98236657
```

The gh token needs the `project` scope (`gh auth refresh -h github.com -s project`).

### Issue Type

Every issue must have its Type set: Bug for defects, Task for consistency and polish, Feature for new behaviour. `gh issue create` can't set it, so after creating, run:

```sh
gh api graphql -f query='mutation($i:ID!,$t:ID!){updateIssue(input:{id:$i,issueTypeId:$t}){issue{issueType{name}}}}' \
  -f i="$(gh issue view N --json id -q .id)" -f t=<type-id>
# Task=IT_kwDOAFwuvc4DI-CI  Bug=IT_kwDOAFwuvc4DI-CJ  Feature=IT_kwDOAFwuvc4DI-CK
```
