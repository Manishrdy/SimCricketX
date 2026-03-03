import resend
resend.api_key = "re_XqaMX2wV_JMGz6Pa4hzKQbkoVwERD35mT"

response = resend.Emails.send({
    "from": "SimCricketX <no-reply@simcricketx.app>",
    "to": "d6mr07@gmail.com",
    "subject": "Test Email",
    "html": "<strong>Your email system works 🎉</strong>"
})

print(response)