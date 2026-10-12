# Premium / Custom Emoji architecture audit

Implemented in this build:
- Editable text overrides persist Telegram `custom_emoji` entities and IDs.
- Central InlineKeyboardButton and KeyboardButton wrappers apply `icon_custom_emoji_id`.
- Reply-keyboard matching tolerates Telegram stripping the fallback emoji.
- Dynamic VIP category and plan names persist Premium Emoji IDs under `text:<name>`.
- VIP category/plan buttons resolve dynamic Premium Emoji IDs.
- Rich editable messages preserve custom-emoji entities through send/edit paths.
- Dynamic plan/category names inside editable templates can restore their custom-emoji entity.
- The user text manager catalog was expanded to include the complete editable-text catalog from the reference build.
- Existing database migrations add `entities_json` without destroying old text overrides.
- Backup export now includes `settings`, which contains button Premium Emoji mappings.


Additional fixes in this build:
- Text editor preserves exact message text/UTF-16 entity offsets instead of stripping it before persistence.
- Persisted custom_emoji entities are validated and retained in `text_overrides.entities_json`.
- Rich editable notification sends use the stored Telegram entities.
- Removed the obsolete `min_gb` variable from the editable text catalog; referral messaging now refers to the first real paid purchase.


## سرویس سفارشی / تست رایگان / تیکت (فیکس‌های اخیر)
تمام متن و دکمه‌های جدید از کاتالوگ متن (`t()` و `answer_rich/edit_rich/send_rich`) استفاده می‌کنند و Premium Emoji در آن‌ها پشتیبانی می‌شود (دسته‌های custom_build، 🎁 تست رایگان، 🎫 تیکت کاربر).


## فیکس‌های این نسخه (نام‌گذاری، سرویس سفارشی، تیکت، توضیحات پوشه)
- **توضیحات پوشه/زیرپوشه**: داخل «🗂 دسته‌بندی‌های VIP ← هر پوشه» دکمه‌های «📝 توضیحات این پوشه» و «🧹 حذف توضیحات این پوشه» اضافه شد. متن با entityهای اصلی تلگرام (Premium Emoji، بولد، لینک) ذخیره می‌شود و بالای لیست همان پوشه به کاربر نشان داده می‌شود. متن‌ها و دکمه‌های این بخش در ویرایشگر متن (دسته «مدیریت دسته‌بندی VIP») هم هستند.
- **نام‌گذاری پلن سفارشی**: هم هنگام افزودن پلن و هم هنگام تغییر نام، Premium Emoji نام ذخیره می‌شود (مثل تست رایگان و پلن‌های VIP).
- **برچسب‌های ترکیبی** (مثل «🟢 باز — #12» یا «نام پلن — ۱۰ گیگ / ۳۰ روز | 🔤 نام»): آیکن Premium بخش پایه حفظ می‌شود، هم روی دکمه‌ها و هم داخل متن فاکتور/خلاصه سفارش.
- **تیکت‌های من**: متن مکالمه (عنوان و برچسب «شما/پشتیبانی») دیگر به str ساده تبدیل نمی‌شود و Premium Emoji آن حفظ می‌شود.
- دسته‌ی ویرایشگر متن «custom_build» با نام فارسی «🛠 سرویس سفارشی» نمایش داده می‌شود.
