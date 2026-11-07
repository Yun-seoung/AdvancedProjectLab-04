from django.db import migrations


def delete_read_notifications(apps, schema_editor):
    """읽은 알림은 이제 지운다 — 그 전에 읽음 처리만 해 두고 남아 있던 행을 정리한다."""
    Notification = apps.get_model("meetalign", "Notification")
    Notification.objects.filter(read_at__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("meetalign", "0025_meeting_participants"),
    ]

    operations = [
        migrations.RunPython(delete_read_notifications, migrations.RunPython.noop),
    ]
