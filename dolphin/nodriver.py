import nodriver as uc

class NoDriverPage:
	def __init__(self, browser):
		self.page = None
		self.browser = browser
	
	def goto(self,url):
		self.page = self.browser.get(url)
		return self.page

	def wait_for_selector(self, selector):
		return self.page.select(selector)

class NoDriverBrowser:
	def __init__(self, port):
		self.config = uc.Config()
		self.config.host = "127.0.0.1"
		self.config.port = port
		self.browser = None
		self.page = None

	async def start(self):
		self.browser = await uc.start(config=self.config)
		self.page = NoDriverPage(self.browser)
