# Trạm Tải Tập Phim

Ứng dụng FastAPI với giao diện tiếng Việt, chạy trên máy của bạn và lưu video trực tiếp vào thư mục bạn chọn.

## Chạy trên Windows

1. Cài [Python 3.10 trở lên](https://www.python.org/downloads/windows/) nếu máy chưa có Python. Bật tùy chọn thêm Python vào PATH khi cài.
2. Giải nén gói ZIP vào thư mục có quyền ghi. Mở `Khoi-dong.cmd`. Lần đầu, chương trình tạo môi trường `.venv` và tải các thư viện Python. Các lần sau dùng lại môi trường này.
3. Khi ứng dụng khởi động, trình duyệt mặc định sẽ mở. Nếu cần, mở thủ công [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Giữ cửa sổ lệnh mở trong lúc tải. Nhấn `Ctrl+C` trong cửa sổ đó để dừng.

Nếu muốn khởi động từ PowerShell, mở PowerShell trong thư mục đã giải nén rồi chạy:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m tramtai
```

Nếu cổng `8765` đã được dùng, chạy lệnh khởi động với cổng khác, ví dụ `.\.venv\Scripts\python.exe -m tramtai --port 8766`.

### FFmpeg cho nguồn HLS

Trong PowerShell, cài FFmpeg bằng lệnh `winget install --id Gyan.FFmpeg -e` hoặc dùng bản dành cho Windows từ trang tải FFmpeg. Với bản giải nén tự cài, thêm thư mục `bin` chứa `ffmpeg.exe` vào PATH. Mở một cửa sổ lệnh mới, chạy `ffmpeg -version` và khởi động lại ứng dụng. Tải trực tiếp MP4, M4V, WebM và MOV chỉ cần Python. [Trang tải FFmpeg](https://ffmpeg.org/download.html#build-windows)

## Tải video

1. Dán link trang công khai hoặc link MP4, M4V, WebM, MOV hay M3U8. Nhập mỗi link một dòng rồi bấm **Tìm tập**. Ứng dụng sắp theo số tập được công bố hoặc nhận ra trong đường dẫn. Các link còn lại được đánh số theo thứ tự xuất hiện, bỏ qua số đã có.
2. Nhập **Tên phim** và đường dẫn đầy đủ của **Thư mục lưu**. Thư mục mặc định là `Downloads\Tram Tai Tap Phim` trong thư mục người dùng và sẽ được tạo khi tải lần đầu. Khi đổi sang đường dẫn khác, hãy chọn một thư mục cục bộ đã có.
3. Dùng **Chọn tất cả**, đánh dấu từng tập hoặc nhập hai số ở **Chọn khoảng** và bấm **Áp dụng**. Hai đầu khoảng đều được tính. Sau đó bấm **Tải các tập đã chọn**.
4. Hàng đợi chạy hai tập đồng thời. Khi nguồn công bố tổng dung lượng, tiến độ hiển thị phần trăm. Khi chưa biết tổng, hàng đợi hiển thị số byte đã nhận. Bạn có thể hủy tác vụ đang tải hoặc đang chờ và thử lại tác vụ đã lỗi hoặc đã hủy. Thử lại tải lại từ đầu.

Tệp hoàn tất có dạng `Tên phim - Tập 01.mp4`. HLS được ghép thành `.mp4`, M4V được lưu nguyên dữ liệu với đuôi `.mp4`, còn WebM và MOV giữ đuôi gốc. Nếu tên đã tồn tại, ứng dụng thêm hậu tố như ` (2)` và giữ nguyên tệp cũ. Dữ liệu đang tải nằm trong thư mục tạm bên trong thư mục lưu. Khi tải hoặc ghép xong, ứng dụng mới đặt tên chính thức. Hàng đợi và URL chỉ tồn tại trong phiên đang chạy.

HLS hỗ trợ danh sách phát hữu hạn, không mã hóa, có dấu kết thúc `EXT-X-ENDLIST`. Ứng dụng chọn biến thể có độ phân giải cao nhất rồi xét băng thông, lấy luồng âm thanh mặc định nếu tách riêng và dùng FFmpeg sao chép các luồng tương thích vào MP4. HLS cần chỗ trống cho cả phân đoạn đã tải và tệp đang ghép.

## Những trang có thể nhận diện tự động

Ứng dụng đọc nguồn `src` hoặc `data-src` trong thẻ `<video>` và `<source>`, liên kết đến tệp video, `VideoObject.contentUrl` hoặc `encoding` trong JSON-LD, và video Open Graph có loại tệp phù hợp. Khi một video khai báo nhiều nguồn thay thế, ứng dụng thử các nguồn công khai và giữ một mục đã xác minh. Nếu nhập một trang có nguồn hợp lệ, ứng dụng có thể gợi ý tên phim từ tiêu đề trang.

Trang chỉ nhúng trình phát, cung cấp nguồn thông qua JavaScript hoặc yêu cầu đăng nhập, quyền truy cập hay DRM cần một link tải trực tiếp được dịch vụ cấp và bạn có quyền dùng. Với danh sách tập phân trang, có thể dán từng trang tập hoặc từng link video. Ứng dụng không dò trang nhúng hay gửi cookie đăng nhập.

## Hai trang đã khảo sát

HTML công khai của trang mẫu trả HTTP 200. Dữ liệu gắn trong trang công bố **1 tập**. Thẻ trình phát HLS có `src` trống. Các trường URL phát và danh sách URL từng tập cũng trống. Dữ liệu có cấu trúc `VideoObject` trỏ đến trang nhúng. [Trang mẫu](https://gkg0.aqkmowihz.cc/video/15372/)

Trang trong ảnh cũng trả HTTP 200 và công bố thông tin một tập. Nguồn của thẻ HLS và các trường URL phát trống. JSON-LD chỉ dẫn đến trang nhúng. Điều khoản của cùng máy chủ hạn chế tải hàng loạt bằng phương tiện kỹ thuật. Hai URL đã khảo sát hiển thị thông báo giới hạn và hướng dẫn nhập link video trực tiếp có quyền tải. [Trang trong ảnh](https://v24t.aqkmowihz.cc/video/15586/) · [Điều khoản](https://v24t.aqkmowihz.cc/terms/)

## Kiểm thử tự động

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
```

Bộ kiểm thử Python cần `ffmpeg` và `ffprobe` trên PATH để chạy toàn bộ. Các bài kiểm thử tự tạo video, phục vụ qua HTTP và xác minh việc nhận diện từ HTML, tải trực tiếp MP4, WebM, MOV và ghép HLS thành MP4. Kiểm thử Node.js 18 trở lên chạy mã JavaScript thật với DOM và API mô phỏng. Giao diện khi mở lần đầu có danh sách tập và hàng đợi trống.
