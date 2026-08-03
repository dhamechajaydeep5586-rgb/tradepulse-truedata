# Generated manually — drops the GrowwHistorySnapshot table (Groww integration removed).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('stocks', '0026_growwhistorysnapshot'),
    ]

    operations = [
        migrations.DeleteModel(
            name='GrowwHistorySnapshot',
        ),
    ]
