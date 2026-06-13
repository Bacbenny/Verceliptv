# Vercel IPTV

Máy chủ IPTV M3U tổng hợp trực tiếp từ TieuLam TV, Hội Quán TV và Khán Đài A — phục vụ playlist `.m3u` và EPG XML, triển khai trên Vercel.

## Run & Operate

- `python main.py` — chạy Flask server (port 5000)
- `pnpm --filter @workspace/api-server run dev` — chạy relay API server (TypeScript/Express)
- `pnpm run typecheck` — kiểm tra kiểu toàn bộ TypeScript
- `pnpm --filter @workspace/api-spec run codegen` — tái tạo hooks và Zod schemas

## Stack

- **Python / Flask** — máy chủ playlist M3U chính (deploy lên Vercel)
- **TypeScript / Express 5** — relay endpoint cho TieuLam API (deploy lên Replit)
- pnpm workspaces, Node.js 24, TypeScript 5.9
- DB: PostgreSQL + Drizzle ORM
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — Flask app chính: fetch + cache + serve M3U playlists và EPG XML
- `requirements.txt` — Python dependencies cho Vercel
- `vercel.json` — cấu hình Vercel deployment
- `artifacts/api-server/src/routes/tieulam-relay.ts` — Express relay bypass IP block cho TieuLam API
- `lib/api-spec/openapi.yaml` — OpenAPI contract

## Architecture decisions

- Playlist được cache in-memory, refresh mỗi 5 phút bằng background thread
- Ba nguồn (TieuLam, HoiQuan, KhanDai) fetch song song bằng `ThreadPoolExecutor`
- Combined playlist `/live.m3u` sắp xếp **tất cả** trận đấu theo giờ thi đấu UTC, bất kể nguồn
- TieuLam relay (TypeScript) được host trên Replit để bypass IP block của Vercel/Render
- Relay trả về kết quả đã sắp xếp theo `start_date` để Python client xử lý nhẹ hơn

## Product

- `/live.m3u` — playlist gộp tất cả nguồn, sắp xếp theo giờ thi đấu
- `/tieulam.m3u`, `/hoiquan.m3u`, `/khandaia.m3u` — playlist riêng từng nguồn
- `/epg.xml` — XMLTV EPG tự sinh từ danh sách kênh
- `/api/tieulam-relay` — relay endpoint lấy dữ liệu TieuLam (dùng khi Vercel bị block IP)

## Environment variables

| Biến | Mô tả | Mặc định |
|------|--------|----------|
| `TIEULAM_RELAY_URL` | URL relay Replit để bypass IP block | (trống = gọi thẳng) |
| `RELAY_SECRET` | Token bảo vệ relay endpoint | (trống = không bảo vệ) |
| `TIEULAM_FRONTEND` | Frontend URL TieuLam | `https://sv1.tieulam1.live` |
| `TIEULAM_API` | API URL TieuLam | `https://api.tlap12062026.xyz` |
| `TIEULAM_CDN` | CDN stream TieuLam | `https://live.secufun.xyz` |
| `HOIQUAN_FRONTEND` | Frontend URL Hội Quán | `https://sv2.hoiquan4.live` |
| `HOIQUAN_API` | API URL Hội Quán | `https://sv.hoiquantv.xyz/api/v1/external` |
| `KHANDAIA_FRONTEND` | Frontend URL Khán Đài A | `https://tructiep.khandaia.link` |
| `KHANDAIA_API` | API URL Khán Đài A | `https://sv.khandai-a.xyz/api/v1/external` |
| `EPG_URL` | Override URL EPG | (tự sinh từ server URL) |
| `MATCH_MAX_DURATION` | Thời gian tối đa 1 trận (giây) | `7200` (2 giờ) |
| `APP_URL` | Public URL của server | (tự phát hiện) |
| `DATABASE_URL` | Postgres connection string | |
| `SESSION_SECRET` | Session secret | |

## Gotchas

- Vercel/Render thường bị Cloudflare block khi gọi TieuLam API — cần set `TIEULAM_RELAY_URL` trỏ tới Replit relay
- `RELAY_SECRET` phải giống nhau ở cả Vercel (client) và Replit (server)
- Playlist combined sắp xếp theo UTC timestamp — hiển thị giờ Việt Nam (UTC+7) trong tên kênh

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
