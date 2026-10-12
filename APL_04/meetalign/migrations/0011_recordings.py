from django.db import migrations, models
import django.db.models.deletion


def copy_single_recordings(apps, schema_editor):
    Meeting = apps.get_model("meetalign", "Meeting")
    Recording = apps.get_model("meetalign", "Recording")
    for meeting in Meeting.objects.exclude(recording__isnull=True).exclude(recording=""):
        status = meeting.processing_status if meeting.processing_status in ("done", "failed") else "done"
        Recording.objects.create(meeting=meeting, file=meeting.recording.name, transcript=meeting.transcript, status=status)


class Migration(migrations.Migration):

    dependencies = [
        ('meetalign', '0010_checkquestion_unique_check_question_order'),
    ]

    operations = [
        migrations.CreateModel(
            name='Recording',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to='recordings/')),
                ('transcript', models.TextField(blank=True)),
                ('status', models.CharField(default='processing', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('meeting', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recordings', to='meetalign.meeting')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.RunPython(copy_single_recordings, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='meeting',
            name='recording',
        ),
    ]
