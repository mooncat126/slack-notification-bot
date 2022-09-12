from os import environ
import hashlib
import hmac

import json
from datetime import datetime
from urllib.request import Request, urlopen

SLACK_CHANNEL_ENDPOINT = "https://hooks.slack.com/services/{YOUR_CHANNEL_ENDPOINT}"
SLACK_API_ENDPOINT = "https://slack.com/api/%s"
MAP_USER_IDS = {
    # GitHubId: SlackID
}
GIT_TRAINING_HOOK_ID = "{YOUR_TEST_CHANNEL_ID}"

def lambda_handler(event, context):

    if not verify_signature(event):
        return create_response(403)

    if is_from_git_traning(event):
        return create_response(200, "git-training repository can not notify to Slack channel.")

    event_data = json.loads(event.get("body", {}))
    action = event_data.get("action")
    repository = event_data.get("repository", {})
    pull_request = event_data.get("pull_request", {})
    isDraft = pull_request.get("draft", False)

    if isDraft:
        return create_response(200, "This pull request is draft: {0}".format(pull_request.get("title")))

    send_data = {}
    if action == "opened" or action == "ready_for_review" or action == "review_requested":
        # PullRequestがオープンされたor ready for review状態になった時の通知
        user = pull_request.get("user", {})
        state = pull_request.get("state", "")

        if state != "open":
            return create_response(200, "This pull request is not yet opened: {0}".format(pull_request.get("title")))
        elif action == "review_requested":
            # 作成日と更新日の差分が小さい場合はオープン時のアクションっぽいので通知しない
            created_at = convert_string_to_date(pull_request.get("created_at"))
            updated_at = convert_string_to_date(pull_request.get("updated_at"))
            if (updated_at - created_at).seconds <= 2:
                return create_response(200, "This review_requested action was created at open.")
        send_data = create_send_data({
            "mentions": map_by_key(pull_request.get("requested_reviewers", []), "login"),
            "mention_message": "さん、PR依頼がきました！\n手が空いてる時に、下記のPRのレビューをお願い致します〜\n",
            "color": "#24292e",
            "user": user,
            "title": pull_request.get("title"),
            "link": pull_request.get("html_url"),
            "text": pull_request.get("body"),
            "repo": repository.get("full_name"),
            "repo_link": repository.get("html_url"),
            "ts": pull_request.get("created_at")
        })
    elif action == "submitted":
        # PullRequetにコメント、Approve、ChangeRequestが来た時の通知
        pull_request = event_data.get("pull_request", {})
        user = pull_request.get("user", {})
        to_user = user.get("login")

        review = event_data.get("review", {})
        submitter = review.get("user", {})
        state = review.get("state")
        if state == "approved":
            color = "#28A745"
            message = "さん、\n下記のPRがApproveされました！\n問題なければマージしてね\n"
        elif state == "commented":
            if submitter.get("login") == to_user:
                return create_response(200, "Sender and creator are same user.")
            color = "#E1E4E8"
            message = "さん、\n下記のPRにコメントがあります！\n"
        elif state == "changes_requested":
            color = "#D73A49"
            message = "さん、\n下記のPRにコメントがあります！\n"

        send_data = create_send_data({
            "mentions": [to_user],
            "mention_message": message,
            "color": color,
            "user": submitter,
            "title": pull_request.get("title"),
            "link": review.get("html_url"),
            "text": review.get("body"),
            "repo": repository.get("full_name"),
            "repo_link": repository.get("html_url"),
            "ts": review.get("submitted_at")
        })
    else:
        return create_response(200, "This GitHub action is not supported: {0}".format(action))

    if not send_data:
        return create_response(200, "サポートしていないユーザがメンションに含まれているか、通知先ユーザーが指定されていません。")

    pay_load = "payload=" + json.dumps(send_data)
    request = Request(
        SLACK_CHANNEL_ENDPOINT,
        data=pay_load.encode("utf-8"),
        method="POST"
    )
    with urlopen(request) as response:
        response_body = response.read().decode("utf-8")

    return {
        "statusCode": 200,
        "body": response_body
    }

def map_by_key(lists, key):
    """
    util系関数
    指定されたkeyでlist内を検索して、一致した要素のvalueのみで新しいlistを作成して返却する
    """
    values = []
    for l in lists:
        v = l.get(key, None)
        if v is not None:
            values.append(v)
    return values

def get_slack_ids(github_ids=[]):
    """
    Slackの表示名をキーにSlackUserIdを取得する
    """
    slack_ids = []

    for github_id in github_ids:
        slack_id = MAP_USER_IDS.get(github_id)
        if slack_id is not None:
            slack_ids.append(slack_id)

    req = Request(
        SLACK_API_ENDPOINT % ("users.list?pretty=1"),
        headers={ "Authorization": "Bearer %s" % environ["SLACK_BOT_API_TOKEN"]},
        method="GET"
    )
    with urlopen(req) as res:
        body = json.loads(res.read().decode("utf-8"))
        members = body.get("members", [])
        members = list(filter(lambda m: m["id"] in slack_ids and not m["deleted"], members))

    return list(map(lambda m: "<@%s>" % m["id"], members))

def create_send_data(data):
    """
    渡されたdataをSlackへPOSTするための形式に整形する
    """
    mentions = data.get("mentions", [])
    send_user = data.get("user", {})
    date = convert_string_to_date(data.get("ts"))
    text = " ".join(get_slack_ids(mentions))

    if not text:
        return

    if "mention_message" in data:
        text += data.get("mention_message")

    return {
        "channel": "#github-notification",
        "username": "github bot",
        "text": text,
        "attachments": [
            {
                "color": data.get("color"),
                "author_name": send_user.get("login"),
                "author_icon": send_user.get("avatar_url"),
                "title": data.get("title"),
                "title_link": data.get("link"),
                "text": data.get("text"),
                "footer": "<{0}|{1}>".format(data.get("repo_link"), data.get("repo")),
                "ts": date.timestamp()
            }
        ]
    }

def verify_signature(e):
    """
    GitHubから渡されたSecretKeyの認証を行う
    localだとなぜかうまく行かない
    """
    header = e.get("headers", {})
    body = e.get("body", {})
    signature = "sha256={}".format(hmac.new(environ["GITHUB_SECRET_TOKEN"].encode('utf-8'), body.encode('utf-8'), hashlib.sha256).hexdigest())
    return hmac.compare_digest(header.get("X-Hub-Signature-256"), signature)

def is_from_git_traning(e):
    """
    GitTraningのWebhooksから発火されたのか判断
    """
    header = e.get("headers", {})
    return str(header.get("X-GitHub-Hook-ID")) == GIT_TRAINING_HOOK_ID

def convert_string_to_date(str_date):
    return datetime.strptime(str_date or "", '%Y-%m-%dT%H:%M:%SZ')

def create_response(status_code, message):
    res = {
        "statusCode": status_code
    }
    if message:
        res["body"] = {
            "message": message
        }

    return res
