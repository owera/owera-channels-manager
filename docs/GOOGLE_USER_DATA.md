# Google / YouTube user data: revocation and deletion

How Channels Manager meets the revocation and deletion parts of the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)
and [YouTube API Services Developer Policies §III.E.4](https://developers.google.com/youtube/terms/developer-policies).

## Disconnect revokes at Google

`POST /api/channels/{id}/disconnect` (and `DELETE /api/channels/{id}` for a
platform channel) calls `youtube.disconnect_channel`:

1. **Platform channels** (`credential_source=platform_oauth`, the CM web OAuth
   client): the stored refresh token is sent to
   `POST https://oauth2.googleapis.com/revoke` (form body, 10 s timeout). This
   ends the grant, so the app disappears from the user's Google security page.
2. Our encrypted copy (`oauthcredential` row) and any legacy token files are
   deleted **whatever the revoke outcome** (5xx, timeout, already invalid,
   undecryptable token).
3. One log line per revoke: `google revoke for channel id=<n> workspace=<n>:
   revoked|already_invalid|no_token|http_<code>|error_<Class>`. The token is
   never logged.

**Legacy channels** (`legacy_file`, ch1/ch2 desktop tokens) are not revoked:
they use the channel owner's own OAuth client outside the CM project, and one
revoke would end that owner's grant for every channel on the same Google
account. Their token files are still deleted on disconnect.

SPA Google sign-in uses `access_type=online` and stores no Google token, so
there is nothing to revoke on sign-out.
