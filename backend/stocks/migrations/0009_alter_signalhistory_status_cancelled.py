from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0008_signalhistory_active_time'),
    ]

    operations = [
        migrations.AlterField(
            model_name='signalhistory',
            name='status',
            field=models.CharField(
                choices=[
                    ('ACTIVE', 'Active'),
                    ('PENDING', 'Pending'),
                    ('CANCELLED', 'Cancelled'),
                    ('HIT_TARGET', 'Hit Target'),
                    ('HIT_SL', 'Hit SL'),
                    ('EXPIRED', 'Expired'),
                ],
                db_index=True,
                default='PENDING',
                max_length=20,
            ),
        ),
    ]
