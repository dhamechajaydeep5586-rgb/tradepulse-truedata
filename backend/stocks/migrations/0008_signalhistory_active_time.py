from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0007_alter_signalhistory_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='signalhistory',
            name='active_time',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
