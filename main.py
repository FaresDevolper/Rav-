import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- الإعدادات -----------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
RATINGS_CHANNEL_ID = int(os.getenv("RATINGS_CHANNEL_ID", "0") or 0)

# رتب الإداريين المسموح لهم باستخدام الأمر (اختياري، افصل بينها بفاصلة).
# لو تركتها فاضية: يكفي أن يملك الشخص صلاحية Manage Messages.
STAFF_ROLE_IDS = {
    int(x) for x in os.getenv("STAFF_ROLE_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

DATA_FILE = Path("pending_ratings.json")
EMBED_COLOR = 0x3B5BA5

SCALE = [(5, "ممتاز"), (4, "جيد جدا"), (3, "جيد"), (2, "مقبول"), (1, "ضعيف")]
CRITERIA = [("style", "الأسلوب"), ("handling", "التعامل"), ("speed", "سرعة الرد")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ticket-rating")


# ----------------------------- التخزين -----------------------------
class PendingStore:
    """يحفظ التقييمات المنتظرة إغلاق التذكرة، وتبقى محفوظة لو أعيد تشغيل البوت."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("تعذر قراءة %s، سيتم البدء بملف فارغ.", path)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set(self, channel_id: int, record: dict) -> None:
        self.data[str(channel_id)] = record
        self._save()

    def pop(self, channel_id: int) -> dict | None:
        record = self.data.pop(str(channel_id), None)
        if record is not None:
            self._save()
        return record


store = PendingStore(DATA_FILE)


# ----------------------------- دوال مساعدة -----------------------------
def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if STAFF_ROLE_IDS:
        return any(role.id in STAFF_ROLE_IDS for role in member.roles)
    return member.guild_permissions.manage_messages


def ticket_number(channel: discord.abc.GuildChannel) -> str:
    """يستخرج رقم التذكرة من اسم الروم (مثل ticket-0042)، وإلا يرجع اسم الروم."""
    numbers = re.findall(r"\d+", channel.name)
    return numbers[-1] if numbers else channel.name


def detect_customer(channel: discord.TextChannel, staff: discord.Member) -> discord.Member | None:
    """يحاول معرفة صاحب التذكرة من صلاحيات الروم (العضو غير الإداري الذي له صلاحية خاصة)."""
    for target in channel.overwrites:
        if (
            isinstance(target, discord.Member)
            and not target.bot
            and target.id != staff.id
            and not is_staff(target)
        ):
            return target
    return None


def prompt_embed(staff: discord.Member, ticket: str) -> discord.Embed:
    embed = discord.Embed(
        title="تقييم الخدمة",
        description=(
            "نشكرك على تواصلك معنا، ونأمل أن نكون قد قدمنا لك الخدمة التي تستحقها.\n\n"
            f"يرجى تقييم الإداري {staff.mention} في البنود الثلاثة التالية، "
            "ثم الضغط على زر إرسال التقييم. رأيك يساعدنا على تطوير مستوى الخدمة."
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(name="بنود التقييم", value="الأسلوب\nالتعامل\nسرعة الرد", inline=True)
    embed.add_field(
        name="مقياس التقييم",
        value="\n".join(f"{n} - {text}" for n, text in SCALE),
        inline=True,
    )
    embed.set_footer(text=f"رقم التذكرة: {ticket}")
    return embed


def thanks_embed(scores: dict[str, int], ticket: str) -> discord.Embed:
    embed = discord.Embed(
        title="تم استلام تقييمك",
        description="نشكرك على وقتك، وتم تسجيل تقييمك بنجاح.",
        color=EMBED_COLOR,
    )
    for key, label in CRITERIA:
        embed.add_field(name=label, value=f"{scores[key]} / 5", inline=True)
    embed.set_footer(text=f"رقم التذكرة: {ticket}")
    return embed


def log_embed(record: dict) -> discord.Embed:
    embed = discord.Embed(title="تقييم جديد", color=EMBED_COLOR)
    embed.timestamp = datetime.fromisoformat(record["rated_at"])
    embed.add_field(name="رقم التذكرة", value=record["ticket"], inline=True)
    embed.add_field(
        name="العميل",
        value=f"<@{record['customer_id']}>\n{record['customer_name']} ({record['customer_id']})",
        inline=True,
    )
    embed.add_field(
        name="الإداري",
        value=f"<@{record['staff_id']}>\n{record['staff_name']} ({record['staff_id']})",
        inline=True,
    )
    for key, label in CRITERIA:
        embed.add_field(name=label, value=f"{record['scores'][key]} / 5", inline=True)
    embed.add_field(name="المعدل العام", value=f"{record['average']:.1f} / 5", inline=True)
    return embed


# ----------------------------- واجهة التقييم -----------------------------
class RatingSelect(discord.ui.Select):
    def __init__(self, key: str, label: str, row: int) -> None:
        options = [
            discord.SelectOption(label=f"{n} - {text}", value=str(n)) for n, text in SCALE
        ]
        super().__init__(
            placeholder=f"{label}: اختر تقييمك",
            options=options,
            min_values=1,
            max_values=1,
            row=row,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "RatingView" = self.view  # type: ignore[assignment]
        if not await view.check_user(interaction):
            return
        view.scores[self.key] = int(self.values[0])
        await interaction.response.defer()


class RatingView(discord.ui.View):
    def __init__(
        self,
        staff: discord.Member,
        customer: discord.Member | None,
        channel: discord.TextChannel,
    ) -> None:
        super().__init__(timeout=None)
        self.staff = staff
        self.customer = customer
        self.channel = channel
        self.ticket = ticket_number(channel)
        self.scores: dict[str, int] = {}
        for row, (key, label) in enumerate(CRITERIA):
            self.add_item(RatingSelect(key, label, row))

    async def check_user(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if self.customer is not None:
            allowed = user.id == self.customer.id
        else:
            allowed = user.id != self.staff.id and not is_staff(user)
        if not allowed:
            await interaction.response.send_message(
                "هذا التقييم مخصص لصاحب التذكرة فقط.", ephemeral=True
            )
        return allowed

    @discord.ui.button(label="إرسال التقييم", style=discord.ButtonStyle.primary, row=3)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.check_user(interaction):
            return
        if len(self.scores) < len(CRITERIA):
            await interaction.response.send_message(
                "يرجى تقييم جميع البنود قبل الإرسال.", ephemeral=True
            )
            return

        average = sum(self.scores.values()) / len(CRITERIA)
        store.set(
            self.channel.id,
            {
                "ticket": self.ticket,
                "customer_id": interaction.user.id,
                "customer_name": str(interaction.user),
                "staff_id": self.staff.id,
                "staff_name": str(self.staff),
                "scores": self.scores,
                "average": average,
                "rated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        await interaction.response.edit_message(
            content=None, embed=thanks_embed(self.scores, self.ticket), view=None
        )
        self.stop()


# ----------------------------- البوت -----------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    if not RATINGS_CHANNEL_ID:
        log.warning("RATINGS_CHANNEL_ID غير محدد في .env، لن تُرسل التقييمات لأي روم.")


@bot.command(name="rate", aliases=["rating", "تقييم"])
@commands.check(lambda ctx: ctx.guild is not None and is_staff(ctx.author))
async def rate(ctx: commands.Context, customer: discord.Member | None = None) -> None:
    """يرسل واجهة التقييم داخل التذكرة. اختياري: منشن صاحب التذكرة."""
    if not isinstance(ctx.channel, discord.TextChannel):
        return
    customer = customer or detect_customer(ctx.channel, ctx.author)
    view = RatingView(staff=ctx.author, customer=customer, channel=ctx.channel)
    await ctx.send(
        content=customer.mention if customer else None,
        embed=prompt_embed(ctx.author, view.ticket),
        view=view,
    )
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """عند إغلاق التذكرة (حذف الروم) يُرسل التقييم لروم السجل."""
    record = store.pop(channel.id)
    if record is None:
        return
    target = bot.get_channel(RATINGS_CHANNEL_ID)
    if target is None:
        try:
            target = await bot.fetch_channel(RATINGS_CHANNEL_ID)
        except discord.HTTPException:
            log.error("تعذر الوصول لروم التقييمات. التقييم لم يُرسل: %s", record)
            return
    try:
        await target.send(embed=log_embed(record))
    except discord.HTTPException:
        log.exception("فشل إرسال التقييم: %s", record)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("ما عندك صلاحية تستخدم هذا الأمر.", delete_after=5)
    elif isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
        await ctx.send("ما قدرت ألقى هذا العضو، تأكد من المنشن.", delete_after=5)
    else:
        log.exception("Unhandled command error", exc_info=error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN غير موجود في ملف .env")
    bot.run(TOKEN)
