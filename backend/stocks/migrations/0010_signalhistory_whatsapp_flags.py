from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0009_alter_signalhistory_status_cancelled'),
    ]

    operations = [
        migrations.AddField(
            model_name='signalhistory',
            name='whatsapp_signal_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='signalhistory',
            name='whatsapp_active_sent',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='signalhistory',
            name='whatsapp_exit_sent',
            field=models.BooleanField(default=False),
        ),
    ]
