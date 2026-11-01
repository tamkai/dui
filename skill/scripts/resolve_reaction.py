import json
import sys


def resolve_reaction(poll, observation):
    if observation.get("message_id") != poll["message_id"]:
        return {"state": "wrong_message", "action": None}
    if not observation.get("complete"):
        return {"state": "unverified", "action": None}
    if observation.get("owner_identity_verified") is not True:
        return {"state": "unverified", "action": None}
    selected = set()
    for reaction in observation.get("reactions", []):
        if reaction.get("emoji") not in poll["choices"]:
            continue
        for voter in reaction.get("voters", []):
            if voter.get("id") == poll["owner_id"] and voter.get("bot") is False:
                selected.add(reaction["emoji"])
    if not selected:
        return {"state": "waiting", "action": None}
    if len(selected) > 1:
        return {"state": "multiple_choices", "action": None}
    emoji = next(iter(selected))
    return {
        "state": "selected",
        "emoji": emoji,
        "action": poll["choices"][emoji],
        "owner_id": poll["owner_id"],
        "message_id": poll["message_id"],
    }


if __name__ == "__main__":
    request = json.load(sys.stdin)
    result = resolve_reaction(request["poll"], request["observation"])
    print(json.dumps(result, ensure_ascii=False))
