# user-skills/

Per-tenant overlay skills. **Always empty in the platform repo.** Populated at runtime inside each tenant's container — only ever written by that tenant via Telegram chat.

Always wins precedence over base `skills/` on name conflict. Platform updates never touch this folder inside any tenant's volume.
