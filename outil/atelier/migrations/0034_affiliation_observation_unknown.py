from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('atelier', '0033_verificationdelivery')]
    operations = [migrations.AlterField(
        model_name='affiliation', name='observed_on',
        field=models.DateField(blank=True, null=True))]
