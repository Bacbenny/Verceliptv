# Verceliptv

IPTV M3U Playlist Server với nhiều nguồn thể thao trực tiếp.

## Nguồn dữ liệu

| Nhóm | URL | Mô tả |
|------|-----|-------|
| TieuLam TV | https://tinhlagi.pro/s.m3u (lọc nhóm "TIẾU LÂM TV") | Bóng đá, bóng rổ, tennis... |
| Hội Quán TV | https://sv2.hoiquan4.live | Thể thao đa dạng |
| Khán Đài A | https://tructiep.khandaia.link | Thể thao trực tiếp |
| **Vòng Cấm TV** | https://sv2.vongcam3.live | Thể thao mới thêm |
| VTV (tĩnh) | - | VTV1-10, Vietnam Today |

## API Endpoints

### Playlist M3U
- `/live.m3u` - Tất cả nguồn gộp lại
- `/tieulam.m3u` - TieuLam TV only
- `/hoiquan.m3u` - Hội Quán TV only
- `/khandaia.m3u` - Khán Đài A only
- `/vongcam.m3u` - Vòng Cấm TV only
- `/vtv.m3u` - Kênh VTV tĩnh

### EPG
- `/epg.xml` - XMLTV tự sinh

## Environment Variables

Xem `.env.example` để biết các biến môi trường cần thiết.

### Vòng Cấm TV
```
VONGCAM_FRONTEND=https://sv2.vongcam3.live/
VONGCAM_API=https://sv.bugiotv.xyz/internal/api/matches
VONGCAM_TOKEN=your-api-token
```

## Deploy

### Vercel
```bash
vercel deploy --prod
```

### Đặt biến môi trường trong Vercel Dashboard hoặc file `.env`

## License

MIT
